"""Acceptance evaluation for canonical external load profiles.

The client always reports the complete HTTP and logical populations.  This
module is the only place where a profile turns those measurements into an
acceptance result.  In particular, stress results never inherit the normal
traffic final-failure SLO.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
import re
from typing import Any


TIMING_EVIDENCE_SCHEMA = 2
MAX_EVIDENCE_COUNT = 1_000_000_000
MAX_EVIDENCE_NUMBER = 1_000_000_000_000.0
_PERCENTILES = ("p50", "p90", "p95", "p99")
_TIMING_METRICS = (
    "service_latency",
    "user_observed_latency",
    "executor_queue_wait",
    "schedule_delay",
    "late_start",
)
_FIXTURE_MARKER_RE = re.compile(r"^preprod[0-9]{12}[0-9a-f]{4}$")
_EXTERNAL_RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")


def derive_expected_phase_plan(
    *,
    mode: Any,
    authored_phase_plan: Any,
    expected_phase_action_counts: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return the closed phase plan for one canonical workload.

    Ready Vote rate ramps carry their authored rate/duration records directly
    in the profile.  Read-mix and page-load profiles intentionally have no
    rate-phase array, but their producers still emit named phase populations;
    derive those names and exact action counts from the profile contract so a
    strict evaluator cannot treat an empty plan as valid evidence.
    """

    if isinstance(authored_phase_plan, (list, tuple)) and authored_phase_plan:
        return [
            dict(item)
            for item in authored_phase_plan
            if isinstance(item, Mapping)
        ]
    if mode not in {"read-mix", "page-load"}:
        return []
    if not isinstance(expected_phase_action_counts, Mapping):
        return []
    return [
        {"name": str(name), "logical_actions": value}
        for name, value in expected_phase_action_counts.items()
        if str(name) not in {"primary", "duplicate", "state", "capacity_ramp"}
    ]


def _number(
    payload: dict[str, Any] | Mapping[str, Any] | None,
    key: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(number) or number < minimum:
            return None
        effective_maximum = (
            MAX_EVIDENCE_NUMBER if maximum is None else maximum
        )
        if number > effective_maximum:
            return None
        return number
    return None


def _observed_latency(summary: Any) -> dict[str, Any] | None:
    """Return the authoritative user-observed latency without a fallback.

    Canonical producers put lifecycle-derived metrics below ``timing``.  A
    duplicated top-level convenience field must never be allowed to override
    that evidence (otherwise a low, service-like value can hide a slow user
    population).  The direct field remains readable only for non-canonical
    diagnostic callers that predate the nested timing schema.
    """

    if not isinstance(summary, dict):
        return None
    timing = summary.get("timing")
    if isinstance(timing, dict) and isinstance(timing.get("user_observed_latency"), dict):
        return timing["user_observed_latency"]
    direct = summary.get("user_observed_latency")
    if isinstance(direct, dict):
        return direct
    return None


def _successful_goodput(
    logical_summary: dict[str, Any],
    raw_http_summary: dict[str, Any],
) -> float | None:
    """Return measured useful goodput without crossing logical/HTTP layers."""

    logical_goodput = _number(
        logical_summary,
        "successful_goodput_actions_per_second",
    )
    if logical_goodput is not None:
        return logical_goodput
    # A report that exposes logical-action accounting must not substitute raw
    # successful responses when its useful-goodput measurement is absent.
    if any(
        key in logical_summary
        for key in (
            "scope",
            "actions",
            "final_successes",
            "final_failures",
            "total_retries",
        )
    ):
        return None
    return _number(raw_http_summary, "successful_goodput_actions_per_second")


def _numeric_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON numbers by value while rejecting bool/string coercion."""

    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        try:
            actual_number = float(actual)
            expected_number = float(expected)
        except (OverflowError, ValueError):
            return False
        return (
            math.isfinite(actual_number)
            and math.isfinite(expected_number)
            and actual_number == expected_number
        )
    return type(actual) is type(expected) and actual == expected


def _finite_number(
    payload: Any,
    key: str,
    *,
    maximum: float | None = None,
) -> float | None:
    return _number(payload, key, maximum=maximum)


def _outcome_consistency(
    summary: Any,
    *,
    required: bool = True,
) -> dict[str, Any]:
    """Recompute reported totals and failure rates from typed counters."""

    if not isinstance(summary, Mapping) or not summary:
        return {
            "present": False,
            "complete": not required,
            "checks": {"summary_present": not required},
        }
    if (
        summary.get("scope") == "logical_user_actions"
        or any(
            key in summary
            for key in ("actions", "final_successes", "final_failures")
        )
    ):
        actions = _nonnegative_int(summary.get("actions"))
        successes = _nonnegative_int(summary.get("final_successes"))
        failures = _nonnegative_int(summary.get("final_failures"))
        actual_rate = _finite_number(
            summary,
            "final_failure_rate_percent",
            maximum=100,
        )
        expected_rate = (
            round(failures * 100 / max(1, actions), 4)
            if actions is not None and failures is not None
            else None
        )
        checks = {
            "typed_actions": actions is not None,
            "typed_final_successes": successes is not None,
            "typed_final_failures": failures is not None,
            "outcomes_do_not_exceed_actions": (
                actions is not None
                and successes is not None
                and failures is not None
                and successes <= actions
                and failures <= actions
            ),
            "outcomes_sum_to_actions": (
                actions is not None
                and successes is not None
                and failures is not None
                and successes + failures == actions
            ),
            "failure_rate_present": actual_rate is not None,
            "failure_rate_recomputed": (
                actual_rate is not None
                and expected_rate is not None
                and actual_rate == expected_rate
            ),
        }
        return {
            "present": True,
            "complete": all(checks.values()),
            "kind": "logical",
            "actions": actions,
            "final_successes": successes,
            "final_failures": failures,
            "expected_failure_rate_percent": expected_rate,
            "actual_failure_rate_percent": actual_rate,
            "checks": checks,
        }
    if (
        summary.get("scope") == "full_population"
        or any(
            key in summary
            for key in ("requests", "errors", "successful_responses")
        )
    ):
        requests = _nonnegative_int(summary.get("requests"))
        errors = _nonnegative_int(summary.get("errors"))
        successful = _nonnegative_int(summary.get("successful_responses"))
        actual_rate = _finite_number(
            summary,
            "final_failure_rate_percent",
            maximum=100,
        )
        expected_rate = (
            round(errors * 100 / max(1, requests), 4)
            if requests is not None and errors is not None
            else None
        )
        checks = {
            "typed_requests": requests is not None,
            "typed_errors": errors is not None,
            "typed_successful_responses": successful is not None,
            "responses_do_not_exceed_requests": (
                requests is not None
                and errors is not None
                and successful is not None
                and errors <= requests
                and successful <= requests
            ),
            "responses_sum_to_requests": (
                requests is not None
                and errors is not None
                and successful is not None
                and errors + successful == requests
            ),
            "failure_rate_present": actual_rate is not None,
            "failure_rate_recomputed": (
                actual_rate is not None
                and expected_rate is not None
                and actual_rate == expected_rate
            ),
        }
        return {
            "present": True,
            "complete": all(checks.values()),
            "kind": "raw_http",
            "requests": requests,
            "errors": errors,
            "successful_responses": successful,
            "expected_failure_rate_percent": expected_rate,
            "actual_failure_rate_percent": actual_rate,
            "checks": checks,
        }
    return {
        "present": False,
        "complete": not required,
        "checks": {"recognized_population_summary": not required},
    }


def _summary_population_reconciliation(summary: Any) -> dict[str, bool]:
    """Tie summary totals to their complete timing population."""

    if not isinstance(summary, Mapping):
        return {"summary_present": False}
    timing = summary.get("timing")
    timing_complete = timing_summary_is_complete(timing)
    if summary.get("scope") == "logical_user_actions" or "actions" in summary:
        count = _nonnegative_int(summary.get("actions"))
        completed = (
            _nonnegative_int(timing.get("completed_count"))
            if isinstance(timing, dict)
            else None
        )
        return {
            "timing_complete": timing_complete,
            "logical_actions_match_timing": (
                count is not None and completed is not None and count == completed
            ),
        }
    count = _nonnegative_int(summary.get("requests"))
    completed = (
        _nonnegative_int(timing.get("completed_count"))
        if isinstance(timing, dict)
        else None
    )
    return {
        "timing_complete": timing_complete,
        "raw_requests_match_timing": (
            count is not None and completed is not None and count == completed
        ),
    }


def _top_population_timing_checks(
    logical_summary: Any,
    raw_http_summary: Any,
) -> dict[str, bool]:
    """Tie every non-empty top-level summary total to its timing boundary."""

    checks: dict[str, bool] = {}
    for label, summary in (("logical", logical_summary), ("raw_http", raw_http_summary)):
        if not isinstance(summary, dict) or not summary:
            continue
        reconciliation = _summary_population_reconciliation(summary)
        if "logical_actions_match_timing" in reconciliation:
            checks[f"top_{label}_actions_match_timing"] = bool(
                reconciliation["logical_actions_match_timing"]
            )
        elif "raw_requests_match_timing" in reconciliation:
            checks[f"top_{label}_requests_match_timing"] = bool(
                reconciliation["raw_requests_match_timing"]
            )
    return checks


def _phase_population_reconciliation(
    phase_summaries: Any,
    logical_summary: Any,
    raw_http_summary: Any,
) -> dict[str, bool]:
    """Tie aggregate action/request totals to the phase populations.

    Ready Vote exposes ``primary`` and ``duplicate`` aggregate phases in
    addition to authored ramp phases; those aggregate phases are the source
    for the top-level action-only HTTP summary.  Other phase reports expose
    their authored phases directly.  State reads are intentionally excluded
    because the top-level Ready Vote raw summary carries them in a separate
    state-read field.
    """

    if not isinstance(phase_summaries, dict) or not phase_summaries:
        return {}
    aggregate_names = [
        name for name in ("primary", "duplicate") if name in phase_summaries
    ]
    if "primary" in phase_summaries:
        phases = [phase_summaries[name] for name in aggregate_names]
    else:
        phases = [
            phase
            for name, phase in phase_summaries.items()
            if str(name) not in {"state", "capacity_ramp"}
            and isinstance(phase, dict)
        ]
    logical_counts: list[int] = []
    raw_counts: list[int] = []
    for phase in phases:
        if not isinstance(phase, dict):
            return {"phase_summaries_reconcilable": False}
        logical = phase.get("logical")
        raw_http = phase.get("raw_http")
        logical_outcome = _outcome_consistency(logical, required=False)
        logical_count = (
            _nonnegative_int(logical_outcome.get("actions"))
            if logical_outcome.get("kind") == "logical"
            else _nonnegative_int(logical_outcome.get("requests"))
            if logical_outcome.get("kind") == "raw_http"
            else None
        )
        raw_count = (
            _nonnegative_int(raw_http.get("requests"))
            if isinstance(raw_http, dict)
            else None
        )
        if logical_count is None or raw_count is None:
            return {"phase_summaries_reconcilable": False}
        logical_counts.append(logical_count)
        raw_counts.append(raw_count)
    top_logical = _outcome_consistency(logical_summary)
    top_raw = _outcome_consistency(raw_http_summary)
    logical_total = (
        top_logical.get("actions")
        if top_logical.get("kind") == "logical"
        else top_logical.get("requests")
    )
    raw_total = top_raw.get("requests") if top_raw.get("kind") == "raw_http" else None
    return {
        "phase_summaries_reconcilable": True,
        "logical_actions_match_phase_totals": (
            logical_total is None or logical_total == sum(logical_counts)
        ),
        "raw_requests_match_phase_totals": (
            raw_total is None or raw_total == sum(raw_counts)
        ),
    }


def _phase_retry_totals_reconciliation(
    phase_summaries: Any,
    logical_summary: Any,
    raw_http_summary: Any,
    *,
    canonical: bool,
) -> dict[str, bool]:
    """Tie aggregate retry counters to the same phase populations."""

    if not canonical or not isinstance(phase_summaries, Mapping) or not phase_summaries:
        return {}
    if "primary" in phase_summaries:
        phases = [
            phase_summaries[name]
            for name in ("primary", "duplicate")
            if name in phase_summaries
        ]
    else:
        phases = [
            phase
            for name, phase in phase_summaries.items()
            if str(name) not in {"state", "capacity_ramp"}
        ]
    phase_logical_retries: list[int] = []
    phase_raw_retries: list[int] = []
    for phase in phases:
        if not isinstance(phase, Mapping):
            return {"phase_retry_totals_reconcilable": False}
        logical = phase.get("logical")
        raw_http = phase.get("raw_http")
        logical_outcome = _outcome_consistency(logical, required=True)
        logical_retry = (
            _nonnegative_int(logical.get("total_retries"))
            if logical_outcome.get("kind") == "logical"
            else _nonnegative_int(logical.get("retry_attempts"))
            if logical_outcome.get("kind") == "raw_http"
            else None
        )
        raw_retry = (
            _nonnegative_int(raw_http.get("retry_attempts"))
            if isinstance(raw_http, Mapping)
            else None
        )
        if logical_retry is None or raw_retry is None:
            return {"phase_retry_totals_reconcilable": False}
        phase_logical_retries.append(logical_retry)
        phase_raw_retries.append(raw_retry)

    # Capacity reports may intentionally keep their aggregate top-level
    # summaries empty and expose only one complete population per rate.  The
    # per-phase retry checks above still apply, while there is no aggregate
    # counter to reconcile in that shape.
    top_logical = _outcome_consistency(logical_summary, required=False)
    top_raw = _outcome_consistency(raw_http_summary, required=False)
    if top_logical.get("kind") not in {"logical", "raw_http"} or top_raw.get(
        "kind"
    ) != "raw_http":
        return {
            "complete": True,
            "phase_retry_totals_reconcilable": True,
            "aggregate_retry_totals_present": False,
        }
    top_logical_retries = (
        _nonnegative_int(logical_summary.get("total_retries"))
        if top_logical.get("kind") == "logical"
        else _nonnegative_int(logical_summary.get("retry_attempts"))
        if top_logical.get("kind") == "raw_http"
        else None
    )
    top_raw_retries = (
        _nonnegative_int(raw_http_summary.get("retry_attempts"))
        if isinstance(raw_http_summary, Mapping)
        else None
    )
    checks = {
        "phase_retry_totals_reconcilable": True,
        "logical_retries_match_phase_totals": (
            top_logical_retries is not None
            and top_logical_retries == sum(phase_logical_retries)
        ),
        "raw_retries_match_phase_totals": (
            top_raw_retries is not None
            and top_raw_retries == sum(phase_raw_retries)
        ),
    }
    return {
        "complete": all(checks.values()),
        "aggregate_retry_totals_present": True,
        **checks,
    }


def _raw_logical_population_reconciliation(
    logical_summary: Any,
    raw_http_summary: Any,
    *,
    max_retries: Any = None,
) -> dict[str, bool]:
    """Ensure the top-level raw population covers the logical population."""

    logical = _outcome_consistency(logical_summary, required=False)
    raw_http = _outcome_consistency(raw_http_summary, required=False)
    logical_count = (
        logical.get("actions")
        if logical.get("kind") == "logical"
        else logical.get("requests")
        if logical.get("kind") == "raw_http"
        else None
    )
    raw_count = raw_http.get("requests") if raw_http.get("kind") == "raw_http" else None
    if logical_count is None or raw_count is None:
        return {}
    retry_limit = _nonnegative_int(max_retries)
    if logical.get("kind") == "raw_http":
        return {
            "raw_requests_reconcile_to_logical_population": (
                raw_count == logical_count
            )
        }
    return {
        "raw_requests_reconcile_to_logical_population": (
            raw_count >= logical_count
            and (
                retry_limit is None
                or raw_count <= logical_count * (retry_limit + 1)
            )
        )
    }


def _duplicate_idempotency_checks(
    duplicate: Any,
    expected_count: int,
) -> dict[str, Any]:
    """Require exact duplicate outcomes with no state-changing response."""

    checks: dict[str, bool] = {
        "phase_present": isinstance(duplicate, dict),
    }
    if not isinstance(duplicate, dict):
        return {"complete": False, "checks": checks}
    logical = duplicate.get("logical")
    raw_http = duplicate.get("raw_http")
    outcome = _outcome_consistency(logical)
    checks["logical_outcome_consistent"] = bool(outcome.get("complete"))
    checks.update(
        {
            f"configured_actions_{expected_count}": (
                _nonnegative_int(duplicate.get("configured_actions")) == expected_count
            ),
            f"submitted_actions_{expected_count}": (
                _nonnegative_int(duplicate.get("submitted_actions")) == expected_count
            ),
            f"completed_actions_{expected_count}": (
                _nonnegative_int(duplicate.get("completed_actions")) == expected_count
            ),
            "missing_actions_zero": _nonnegative_int(
                duplicate.get("missing_actions")
            ) == 0,
            "complete_flag": duplicate.get("complete") is True,
        }
    )
    changed_counts = logical.get("changed_counts") if isinstance(logical, dict) else None
    changed_counts = changed_counts if isinstance(changed_counts, dict) else {}
    changed_keys_valid = (
        all(key in {"False", "True"} for key in changed_counts)
        and (expected_count == 0 or "False" in changed_counts)
    )
    changed_false = _nonnegative_int(changed_counts.get("False", 0))
    changed_true = _nonnegative_int(changed_counts.get("True", 0))
    successes = _nonnegative_int(logical.get("final_successes")) if isinstance(logical, dict) else None
    checks.update(
        {
            "changed_counts_present": isinstance(
                logical.get("changed_counts") if isinstance(logical, dict) else None,
                dict,
            ),
            "changed_counts_typed": (
                changed_keys_valid and changed_false is not None and changed_true is not None
            ),
            "duplicate_has_no_state_changes": (
                changed_keys_valid
                and changed_false is not None
                and changed_true == 0
                and successes is not None
                and changed_false == successes
            ),
        }
    )
    raw_outcome = _outcome_consistency(raw_http)
    checks["raw_outcome_consistent"] = bool(raw_outcome.get("complete"))
    checks["raw_unexpected_statuses_zero"] = (
        isinstance(raw_http, dict)
        and _nonnegative_int(raw_http.get("unexpected_statuses")) == 0
    )
    return {
        "complete": all(checks.values()),
        "checks": checks,
        "logical_outcome": outcome,
        "raw_outcome": raw_outcome,
    }


def _state_read_evidence(
    state: Any,
    expected_count: int,
    *,
    canonical: bool = False,
    expected_primary_successes: int | None = None,
    expected_statuses: frozenset[int] | None = None,
) -> dict[str, Any]:
    """Require one complete, authoritative Ready Vote state-read population."""

    if _nonnegative_int(expected_count) is None:
        return {
            "complete": False,
            "checks": {"expected_count_typed": False},
        }
    if not isinstance(state, dict):
        return {
            "complete": False,
            "checks": {"phase_present": False},
        }
    timing = state.get("timing")
    state_outcome = _outcome_consistency(state)
    state_population = normalize_evidence(
        state,
        expected_scope="full_population",
        required=True,
        canonical=canonical,
        require_goodput=False,
        label="state_read",
        expected_statuses=expected_statuses,
    )
    readiness = state.get("ready_count_evidence")
    readiness = readiness if isinstance(readiness, dict) else {}
    expected = readiness.get("expected")
    observed = readiness.get("observed")
    expected = expected if isinstance(expected, dict) else {}
    observed = observed if isinstance(observed, dict) else {}
    map_types = all(
        isinstance(key, str) and _nonnegative_int(value) is not None
        for mapping in (expected, observed)
        for key, value in mapping.items()
    )
    checks = {
        "phase_present": True,
        "configured_reads": _nonnegative_int(state.get("configured_reads")) == expected_count,
        "submitted_reads": _nonnegative_int(state.get("submitted_reads")) == expected_count,
        "completed_reads": _nonnegative_int(state.get("completed_reads")) == expected_count,
        "raw_requests": _nonnegative_int(state.get("requests")) == expected_count,
        "missing_reads_zero": _nonnegative_int(state.get("missing_reads")) == 0,
        "complete_flag": state.get("complete") is True,
        "timing_complete": timing_summary_is_complete(timing),
        "raw_population_complete": bool(state_population.get("complete")),
        "raw_outcome_consistent": bool(state_outcome.get("complete")),
        "raw_requests_match_timing": (
            isinstance(timing, dict)
            and _nonnegative_int(timing.get("completed_count")) == expected_count
            and _nonnegative_int(timing.get("expected_count")) == expected_count
        ),
        "errors_zero": _nonnegative_int(state.get("errors")) == 0,
        "unexpected_statuses_zero": _nonnegative_int(state.get("unexpected_statuses")) == 0,
        "authoritative": state.get("authoritative") is True,
        "ready_count_evidence_present": (
            readiness.get("complete") is True
            and isinstance(readiness.get("mismatches"), int)
            and not isinstance(readiness.get("mismatches"), bool)
            and readiness.get("mismatches") == 0
        ),
        "ready_count_maps_typed": map_types,
        "ready_count_maps_match": (
            (bool(expected) or expected_count == 0)
            and set(expected) == set(observed)
            and len(expected) == expected_count
        ),
        "ready_count_totals_match_primary": (
            not canonical
            or (
                expected_primary_successes is not None
                and map_types
                and sum(expected.values()) == expected_primary_successes
                and sum(observed.values()) == expected_primary_successes
            )
        ),
    }
    return {
        "complete": all(checks.values()),
        "checks": checks,
        "expected": expected,
        "observed": observed,
        "outcome": state_outcome,
        "population": state_population,
    }


def _budget_check(
    checks: dict[str, bool],
    name: str,
    actual: float | None,
    budget: float | None,
) -> None:
    if budget is None:
        return
    checks[name] = actual is not None and actual <= budget


def _acceptance_budget_evidence(
    contract: Any,
    *,
    require_statuses: bool = False,
) -> dict[str, Any]:
    """Validate authored acceptance budget values without coercion.

    Profile loading performs the authoritative schema validation.  This
    second, evaluator-side guard keeps direct canonical callers fail-closed if
    they construct a contract containing a negative, non-finite, boolean or
    string budget value after profile selection.
    """

    if not isinstance(contract, Mapping):
        return {"complete": False, "checks": {"acceptance_contract_mapping": False}}
    checks: dict[str, bool] = {}

    def numeric_tree_is_valid(value: Any) -> bool:
        if isinstance(value, Mapping):
            return all(numeric_tree_is_valid(child) for child in value.values())
        if isinstance(value, list):
            return all(numeric_tree_is_valid(child) for child in value)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True
        return _number({"value": value}, "value") is not None

    checks["numeric_budget_tree"] = numeric_tree_is_valid(contract)

    statuses = contract.get("expected_statuses")
    if statuses is None and not require_statuses:
        pass
    else:
        statuses_typed = (
            isinstance(statuses, list)
            and bool(statuses)
            and all(
                isinstance(status, int)
                and not isinstance(status, bool)
                and 100 <= status <= 599
                for status in statuses
            )
        )
        checks["expected_statuses"] = (
            statuses_typed
            and len(set(statuses)) == len(statuses)
        )
    if "unexpected_statuses" in contract:
        checks["unexpected_statuses_budget"] = (
            _nonnegative_int(contract.get("unexpected_statuses")) is not None
        )

    scopes: list[tuple[str, Mapping[str, Any]]] = [("acceptance", contract)]
    slo = contract.get("slo")
    if isinstance(slo, Mapping):
        scopes.append(("acceptance.slo", slo))
    resource_safety = contract.get("resource_safety")
    if isinstance(resource_safety, Mapping):
        # Resource budgets are consumed by the observer branch rather than the
        # traffic branch, but they are still untrusted numeric contract input.
        # Include them in the same strict parser pass so a string/boolean
        # value cannot silently disable an origin-safety check.
        scopes.append(("acceptance.resource_safety", resource_safety))
    for prefix, mapping in scopes:
        for key, maximum in (
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
            if key in mapping:
                checks[f"{prefix}.{key}"] = (
                    _number(mapping, key, maximum=maximum) is not None
                )
        for key, percentiles in (
            ("accepted_request_latency", _PERCENTILES),
            ("logical_latency", ("p95", "p99")),
            ("pool_checkout_wait_ms", ("p95", "p99")),
        ):
            budget = mapping.get(key)
            if budget is None:
                continue
            if not isinstance(budget, Mapping):
                checks[f"{prefix}.{key}_mapping"] = False
                continue
            previous = -1.0
            for percentile in percentiles:
                value = _number(budget, f"{percentile}_ms")
                checks[f"{prefix}.{key}.{percentile}_ms"] = (
                    value is not None and value >= previous
                )
                if value is not None:
                    previous = value
    capacity = contract.get("capacity")
    if isinstance(capacity, Mapping):
        rates = capacity.get("target_logical_actions_per_second")
        if rates is not None:
            checks["acceptance.capacity.target_rates"] = (
                isinstance(rates, list)
                and all(
                    _number({"value": value}, "value", maximum=512) is not None
                    for value in rates
                )
            )
        if "steady_duration_seconds" in capacity:
            checks["acceptance.capacity.steady_duration_seconds"] = (
                _number(capacity, "steady_duration_seconds") is not None
            )
    return {"complete": all(checks.values()), "checks": checks}


_TIMING_COUNT_FIELDS = (
    "expected_count",
    "submitted_count",
    "completed_count",
    "scheduled_count",
    "started_count",
    "actual_request_start_count",
    "actual_start_count",
    "response_completion_count",
    "user_observed_count",
)


def _nonnegative_int(
    value: Any,
    *,
    maximum: int = MAX_EVIDENCE_COUNT,
) -> int | None:
    """Return a real non-negative integer, excluding bool-as-int values."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        return None
    return value


def _timing_metric_checks(
    timing: Any,
    *,
    require_user_observed: bool = False,
) -> dict[str, bool]:
    """Validate nested latency metric populations and percentile domains."""

    if not isinstance(timing, Mapping):
        return {"timing_metrics_mapping": False}
    population = _nonnegative_int(timing.get("completed_count"))
    if population is None:
        return {"timing_metric_population_typed": False}
    checks: dict[str, bool] = {
        "timing_schema": timing.get("timing_schema") == TIMING_EVIDENCE_SCHEMA,
    }
    if require_user_observed:
        checks["user_observed_latency_present"] = isinstance(
            timing.get("user_observed_latency"), Mapping
        )
    for metric_name in _TIMING_METRICS:
        metric = timing.get(metric_name)
        if metric is None:
            checks[f"{metric_name}_present"] = False
            continue
        if not isinstance(metric, Mapping):
            checks[f"{metric_name}_mapping"] = False
            continue
        metric_count = _nonnegative_int(metric.get("count"))
        checks[f"{metric_name}_count"] = (
            metric_count is not None and metric_count == population
        )
        values: list[float] = []
        for percentile in _PERCENTILES:
            value = _number(metric, f"{percentile}_ms")
            if population == 0:
                checks[f"{metric_name}_{percentile}_empty"] = (
                    f"{percentile}_ms" in metric and metric.get(f"{percentile}_ms") is None
                )
            else:
                checks[f"{metric_name}_{percentile}_finite"] = (
                    f"{percentile}_ms" in metric and value is not None
                )
                if value is not None:
                    values.append(value)
        if population > 0:
            checks[f"{metric_name}_percentiles_ordered"] = (
                len(values) == len(_PERCENTILES)
                and all(left <= right for left, right in zip(values, values[1:]))
            )
        for field in ("avg_ms", "max_ms"):
            if field in metric:
                checks[f"{metric_name}_{field}_finite"] = (
                    metric.get(field) is None
                    if population == 0
                    else _number(metric, field) is not None
                )
    return checks


def _timing_evidence_is_complete(
    timing: Any,
    *,
    require_metrics: bool = False,
) -> bool:
    checks = _timing_metric_checks(
        timing,
        require_user_observed=require_metrics,
    ) if require_metrics else {}
    return timing_summary_is_complete(timing) and all(checks.values())


def timing_summary_is_complete(
    timing: Any,
    *,
    require_metrics: bool = False,
) -> bool:
    """Validate the canonical timing population, fail closed.

    A latency percentile is not evidence that the whole offered workload was
    observed.  Every current report therefore carries one count for each
    boundary of the request/action lifecycle.  All counts must agree, the
    producer must explicitly report ``partial: false``, and no work may be
    dropped.  This helper is intentionally independent of a profile kind so it
    can be applied uniformly to SLO, stress, spike and capacity phases.
    """

    if not isinstance(timing, Mapping):
        return False
    if timing.get("partial") is not False:
        return False
    for field in (
        "dropped_work",
        "missing_schedule_context",
        "missing_timing_context",
        "invalid_timing_context",
    ):
        if _nonnegative_int(timing.get(field)) != 0:
            return False
    counts = {
        field: _nonnegative_int(timing.get(field))
        for field in _TIMING_COUNT_FIELDS
    }
    if any(value is None for value in counts.values()):
        return False
    if len(set(counts.values())) != 1:
        return False
    if require_metrics:
        return all(
            _timing_metric_checks(timing, require_user_observed=True).values()
        )
    return True


def _timing_summary_details(
    timing: Any,
    *,
    require_metrics: bool = False,
) -> dict[str, Any]:
    """Return compact diagnostics for a failed timing completeness check."""

    if not isinstance(timing, dict):
        return {
            "present": False,
            "complete": False,
            "reason": "missing_timing_summary",
        }
    details: dict[str, Any] = {
        "present": True,
        "complete": timing_summary_is_complete(timing, require_metrics=require_metrics),
        "partial": timing.get("partial"),
        "dropped_work": timing.get("dropped_work"),
        "missing_schedule_context": timing.get("missing_schedule_context"),
        "counts": {field: timing.get(field) for field in _TIMING_COUNT_FIELDS},
    }
    if details["complete"] is False:
        details["reason"] = (
            "partial_or_dropped_work"
            if timing.get("partial") is not False or timing.get("dropped_work") != 0
            else "missing_or_inconsistent_timing_counts"
        )
    return details


def _status_distribution_checks(
    summary: Any,
    *,
    count: int | None,
    field: str,
    expected_statuses: frozenset[int] | None = None,
) -> dict[str, bool]:
    """Validate the status distribution for one normalized population."""

    distribution = summary.get(field) if isinstance(summary, Mapping) else None
    if not isinstance(distribution, Mapping):
        return {f"{field}_present": False}
    parsed_statuses: list[tuple[int, int]] = []
    typed = True
    for key, value in distribution.items():
        try:
            status = int(key) if isinstance(key, str) and key.isdigit() else None
        except (TypeError, ValueError):
            status = None
        count_value = _nonnegative_int(value)
        if status is None or status < 0 or count_value is None:
            typed = False
            continue
        parsed_statuses.append((status, count_value))
    total = sum(
        _nonnegative_int(value) or 0
        for value in distribution.values()
        if _nonnegative_int(value) is not None
    )
    if field == "final_status_counts":
        successful = _nonnegative_int(summary.get("final_successes"))
        failed = _nonnegative_int(summary.get("final_failures"))
    else:
        successful = _nonnegative_int(summary.get("successful_responses"))
        failed = _nonnegative_int(summary.get("errors"))
    status_successes = sum(
        count for status, count in parsed_statuses if 200 <= status < 400
    )
    status_failures = sum(
        count for status, count in parsed_statuses if not 200 <= status < 400
    )
    checks = {
        f"{field}_present": True,
        f"{field}_typed": typed,
        f"{field}_matches_population": (
            typed and count is not None and total == count
        ),
        f"{field}_matches_expected_statuses": (
            expected_statuses is None
            or all(status in expected_statuses for status, _count in parsed_statuses)
        ),
    }
    checks[f"{field}_matches_outcomes"] = (
        typed
        and successful is not None
        and failed is not None
        and status_successes == successful
        and status_failures == failed
    )
    return checks


def _expected_statuses(contract: Any) -> frozenset[int] | None:
    """Read the profile's status allowlist without coercing its values."""

    if not isinstance(contract, Mapping):
        return None
    statuses = contract.get("expected_statuses")
    if not isinstance(statuses, (list, tuple)):
        return None
    if any(isinstance(status, bool) or not isinstance(status, int) for status in statuses):
        return None
    return frozenset(statuses)


def _derived_goodput(
    summary: Any,
    outcome: Mapping[str, Any],
    *,
    require_measurement: bool,
) -> dict[str, Any]:
    """Recompute successful goodput from final outcomes and measured elapsed time."""

    successes = (
        outcome.get("final_successes")
        if outcome.get("kind") == "logical"
        else outcome.get("successful_responses")
        if outcome.get("kind") == "raw_http"
        else None
    )
    elapsed = _number(summary, "wall_seconds")
    elapsed_field = "wall_seconds" if elapsed is not None else None
    if elapsed is None and isinstance(summary, Mapping):
        timing = summary.get("timing")
        if isinstance(timing, Mapping):
            for field in (
                "elapsed_seconds",
                "offered_arrival_window_seconds",
                "response_completion_window_seconds",
                "actual_arrival_window_seconds",
            ):
                candidate = _number(timing, field)
                if candidate is not None and candidate > 0:
                    elapsed = candidate
                    elapsed_field = f"timing.{field}"
                    break
    reported = _number(summary, "successful_goodput_actions_per_second")
    derived = (
        float(successes) / elapsed
        if _nonnegative_int(successes) is not None and elapsed is not None and elapsed > 0
        else None
    )
    # Producer values are rounded to three decimals.  Keep the tolerance tied
    # to that serialization precision while making a fabricated positive value
    # impossible when no successful outcome exists.
    tolerance = (
        max(0.01, abs(derived) * 0.001)
        if derived is not None
        else None
    )
    match = (
        reported is not None
        and derived is not None
        and abs(reported - derived) <= tolerance
        and (derived > 0 or reported == 0)
    )
    checks = {
        "wall_seconds_valid": (
            not isinstance(summary, Mapping)
            or "wall_seconds" not in summary
            or (_number(summary, "wall_seconds") is not None and elapsed_field == "wall_seconds")
        ),
        "goodput_elapsed_present": elapsed is not None and elapsed > 0,
        "goodput_successes_typed": _nonnegative_int(successes) is not None,
        "goodput_derived": derived is not None,
        "goodput_reported_present": reported is not None,
        "goodput_matches_derived": match,
    }
    if not require_measurement:
        checks = {
            "goodput_reported_nonnegative": reported is None or reported >= 0,
            "goodput_reported_reconciled": reported is None or match,
        }
    return {
        "complete": all(checks.values()),
        "checks": checks,
        "successes": successes,
        "elapsed_seconds": elapsed,
        "elapsed_field": elapsed_field,
        "reported": reported,
        "derived": derived,
        "tolerance": tolerance,
    }


def _scope_shape_check(summary: Mapping[str, Any], expected_scope: str) -> bool:
    """Reject mixed logical/raw population fields in a canonical envelope."""

    logical_only = {
        "actions",
        "final_successes",
        "final_failures",
        "final_status_counts",
    }
    raw_only = {
        "requests",
        "errors",
        "successful_responses",
        "status_counts",
        "retry_attempts",
        "unexpected_statuses",
        "temporary_overload_responses",
        "temporary_overload_rate_percent",
    }
    if expected_scope == "logical_user_actions":
        return not any(key in summary for key in raw_only)
    if expected_scope == "full_population":
        return not any(key in summary for key in logical_only)
    return False


def normalize_evidence(
    summary: Any,
    *,
    expected_scope: str,
    required: bool = True,
    canonical: bool = False,
    require_goodput: bool = True,
    label: str = "summary",
    expected_statuses: frozenset[int] | None = None,
) -> dict[str, Any]:
    """Normalize one untrusted population into a closed-world evidence record.

    Canonical reports must identify their population scope explicitly.  The
    normalized record is then reused by outcome, timing, status, goodput and
    retry checks so no evaluator branch can silently substitute one layer for
    another.
    """

    present = isinstance(summary, Mapping) and bool(summary)
    if not present:
        return {
            "present": False,
            "complete": not required,
            "scope": None,
            "kind": "logical" if expected_scope == "logical_user_actions" else "raw_http",
            "checks": {f"{label}_present": not required},
        }
    outcome = _outcome_consistency(summary, required=required)
    kind = "logical" if expected_scope == "logical_user_actions" else "raw_http"
    count = (
        outcome.get("actions")
        if kind == "logical"
        else outcome.get("requests")
    )
    checks: dict[str, bool] = {
        f"{label}_scope": summary.get("scope") == expected_scope
        if canonical
        else True,
        f"{label}_scope_shape": (
            _scope_shape_check(summary, expected_scope) if canonical else True
        ),
        f"{label}_outcome": bool(outcome.get("complete")),
    }
    if kind == "logical":
        if canonical:
            checks.update(
                _status_distribution_checks(
                    summary,
                    count=count,
                    field="final_status_counts",
                    expected_statuses=expected_statuses,
                )
            )
        checks["logical_retry_count_typed"] = _nonnegative_int(
            summary.get("total_retries")
        ) is not None
        retry_count = _nonnegative_int(summary.get("total_retries"))
        reported_retry_percent = _number(summary, "retry_amplification_percent")
        expected_retry_percent = (
            round(retry_count * 100 / max(1, count), 4)
            if retry_count is not None and _nonnegative_int(count) is not None
            else None
        )
        checks.update(
            {
                "logical_retry_amplification_present": (
                    reported_retry_percent is not None
                ),
                "logical_retry_amplification_recomputed": (
                    reported_retry_percent is not None
                    and expected_retry_percent is not None
                    and reported_retry_percent == expected_retry_percent
                ),
            }
        )
    else:
        if canonical:
            checks.update(
                _status_distribution_checks(
                    summary,
                    count=count,
                    field="status_counts",
                    expected_statuses=expected_statuses,
                )
            )
            requests = _nonnegative_int(outcome.get("requests"))
            errors = _nonnegative_int(outcome.get("errors"))
            temporary_overloads = _nonnegative_int(
                summary.get("temporary_overload_responses")
            )
            overload_rate = _number(
                summary,
                "temporary_overload_rate_percent",
                maximum=100,
            )
            expected_overload_rate = (
                round(temporary_overloads * 100 / max(1, requests), 4)
                if temporary_overloads is not None and requests is not None
                else None
            )
            unexpected_statuses = _nonnegative_int(
                summary.get("unexpected_statuses")
            )
            checks.update(
                {
                    "raw_temporary_overloads_typed": (
                        temporary_overloads is not None
                    ),
                    "raw_temporary_overloads_do_not_exceed_errors": (
                        temporary_overloads is not None
                        and errors is not None
                        and temporary_overloads <= errors
                    ),
                    "raw_overload_rate_recomputed": (
                        overload_rate is not None
                        and expected_overload_rate is not None
                        and overload_rate == expected_overload_rate
                    ),
                    "raw_unexpected_statuses_recomputed": (
                        unexpected_statuses is not None
                        and errors is not None
                        and temporary_overloads is not None
                        and unexpected_statuses == errors - temporary_overloads
                    ),
                }
            )
        for field in ("unexpected_statuses", "retry_attempts"):
            checks[f"raw_{field}_typed"] = _nonnegative_int(summary.get(field)) is not None
        if canonical:
            checks["raw_unexpected_statuses_zero"] = (
                _nonnegative_int(summary.get("unexpected_statuses")) == 0
            )
        if canonical:
            retry_attempts = _nonnegative_int(summary.get("retry_attempts"))
            total_retries = _nonnegative_int(summary.get("total_retries"))
            checks["raw_total_retries_typed"] = total_retries is not None
            checks["raw_total_retries_matches_attempts"] = (
                retry_attempts is not None
                and total_retries is not None
                and total_retries == retry_attempts
            )
        checks["raw_overload_rate_bounded"] = (
            _number(summary, "temporary_overload_rate_percent", maximum=100) is not None
        )
    timing = summary.get("timing") if isinstance(summary, Mapping) else None
    checks["timing_complete"] = _timing_evidence_is_complete(
        timing,
        require_metrics=canonical,
    )
    goodput = _derived_goodput(
        summary,
        outcome,
        require_measurement=canonical and require_goodput,
    )
    checks["goodput_evidence"] = (
        goodput["complete"] if require_goodput else goodput["complete"]
    )
    return {
        "present": True,
        "complete": all(checks.values()),
        "scope": summary.get("scope"),
        "kind": kind,
        "count": count,
        "outcome": outcome,
        "timing": timing,
        "goodput": goodput,
        "checks": checks,
    }


def _capacity_ramp_evidence(
    ramp: Any,
    expected_stage_counts: Mapping[str, Any] | None,
    *,
    canonical: bool,
    expected_statuses: frozenset[int] | None = None,
    acceptance_contract: Mapping[str, Any] | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """Validate every read-mix concurrency stage as its own population.

    The ramp container has no aggregate workload of its own.  Treating it as
    one would allow a good aggregate to hide an empty or partial stage, so the
    selected profile's stage keys and per-stage population are checked before
    acceptance evaluates the aggregate report.
    """

    if expected_stage_counts is None:
        return {"complete": True, "checks": {}, "stages": {}}
    if not isinstance(expected_stage_counts, Mapping):
        return {
            "complete": False,
            "checks": {"expected_stage_counts_mapping": False},
            "stages": {},
        }
    if not expected_stage_counts:
        return {
            "complete": False,
            "checks": {"expected_stage_counts_nonempty": False},
            "stages": {},
        }
    stage_contract: Mapping[str, Any] = acceptance_contract or {}
    if (
        stage_contract.get("kind") == "capacity"
        and isinstance(stage_contract.get("slo"), Mapping)
    ):
        stage_contract = dict(stage_contract["slo"])
        if "expected_statuses" in (acceptance_contract or {}):
            stage_contract = {
                **stage_contract,
                "expected_statuses": (acceptance_contract or {})[
                    "expected_statuses"
                ],
            }
        if "minimum_useful_goodput_actions_per_second" in (acceptance_contract or {}):
            stage_contract = {
                **stage_contract,
                "minimum_useful_goodput_actions_per_second": (
                    acceptance_contract or {}
                )["minimum_useful_goodput_actions_per_second"],
            }
    stages = ramp.get("stages") if isinstance(ramp, Mapping) else None
    expected_keys = {str(key) for key in expected_stage_counts}
    authored_stages = ramp.get("concurrency_stages") if isinstance(ramp, Mapping) else None
    checks: dict[str, bool] = {
        "ramp_present": isinstance(ramp, Mapping),
        "ramp_authored_stage_plan": (
            isinstance(authored_stages, list)
            and all(
                isinstance(stage, int) and not isinstance(stage, bool)
                for stage in authored_stages
            )
            and [str(stage) for stage in authored_stages]
            == [str(stage) for stage in expected_stage_counts]
        ),
        "ramp_stages_present": isinstance(stages, Mapping),
        "ramp_stage_names_closed": (
            isinstance(stages, Mapping) and set(str(key) for key in stages) == expected_keys
        ),
        "ramp_stage_order": (
            isinstance(stages, Mapping)
            and list(str(key) for key in stages)
            == list(str(key) for key in expected_stage_counts)
        ),
    }
    stage_results: dict[str, Any] = {}
    if not isinstance(stages, Mapping):
        return {"complete": False, "checks": checks, "stages": stage_results}
    for stage_name, expected_count_value in expected_stage_counts.items():
        name = str(stage_name)
        expected_count = _nonnegative_int(expected_count_value)
        summary = stages.get(name)
        evidence = normalize_evidence(
            summary,
            expected_scope="full_population",
            required=True,
            canonical=canonical,
            require_goodput=True,
            label=f"ramp_{name}",
            expected_statuses=expected_statuses,
        )
        timing = summary.get("timing") if isinstance(summary, Mapping) else None
        checks[f"ramp_{name}_expected_count_typed"] = expected_count is not None
        checks[f"ramp_{name}_population"] = (
            bool(evidence.get("complete"))
            and expected_count is not None
            and _nonnegative_int(summary.get("requests")) == expected_count
            and isinstance(timing, Mapping)
            and _nonnegative_int(timing.get("expected_count")) == expected_count
            and _nonnegative_int(timing.get("submitted_count")) == expected_count
            and _nonnegative_int(timing.get("completed_count")) == expected_count
        )
        stage_budget = _phase_budget_checks(
            f"capacity_ramp_{name}",
            {"logical": summary, "raw_http": summary},
            dict(stage_contract),
            max_retries=max_retries,
            canonical=canonical,
            expected_logical_scope="full_population",
        )
        checks[f"ramp_{name}_budgets"] = bool(stage_budget.get("passed"))
        evidence["budget_evidence"] = stage_budget
        stage_results[name] = evidence
    return {
        "complete": all(checks.values()),
        "checks": checks,
        "stages": stage_results,
    }


def _retry_reconciliation(
    logical_summary: Any,
    raw_http_summary: Any,
    *,
    canonical: bool,
) -> dict[str, Any]:
    """Reconcile raw attempts with logical work and reported retry counts."""

    logical = logical_summary if isinstance(logical_summary, Mapping) else {}
    raw_http = raw_http_summary if isinstance(raw_http_summary, Mapping) else {}
    logical_outcome = _outcome_consistency(logical, required=canonical)
    raw_outcome = _outcome_consistency(raw_http, required=canonical)
    if logical_outcome.get("kind") == "logical":
        base_actions = _nonnegative_int(logical_outcome.get("actions"))
        retries = _nonnegative_int(logical.get("total_retries"))
        reported_amplification = _number(logical, "retry_amplification_percent")
    elif logical_outcome.get("kind") == "raw_http":
        base_requests = _nonnegative_int(logical_outcome.get("requests"))
        retries = _nonnegative_int(logical.get("retry_attempts"))
        base_actions = (
            base_requests - retries
            if base_requests is not None and retries is not None
            else None
        )
        reported_amplification = _number(logical, "retry_amplification_percent")
    else:
        base_actions = None
        retries = None
        reported_amplification = None
    raw_requests = _nonnegative_int(raw_outcome.get("requests"))
    raw_retry_attempts = _nonnegative_int(raw_http.get("retry_attempts"))
    expected_amplification = (
        round(retries * 100 / max(1, base_actions), 4)
        if retries is not None and base_actions is not None
        else None
    )
    checks = {
        "base_logical_actions_typed": base_actions is not None,
        "retry_count_typed": retries is not None,
        "raw_requests_typed": raw_requests is not None,
        "raw_retry_attempts_typed": raw_retry_attempts is not None,
        "raw_retry_attempts_match_logical": (
            retries is not None
            and raw_retry_attempts is not None
            and retries == raw_retry_attempts
        ),
        "raw_requests_equal_base_plus_retries": (
            raw_requests is not None
            and base_actions is not None
            and retries is not None
            and base_actions >= 0
            and raw_requests == base_actions + retries
        ),
        "logical_retry_amplification_recomputed": (
            reported_amplification is not None
            and expected_amplification is not None
            and reported_amplification == expected_amplification
        ),
    }
    raw_reported_amplification = _number(raw_http, "retry_amplification_percent")
    if canonical:
        checks["raw_retry_amplification_recomputed"] = (
            raw_reported_amplification is not None
            and expected_amplification is not None
            and raw_reported_amplification == expected_amplification
        )
    return {
        "complete": all(checks.values()) if canonical else True,
        "checks": checks,
        "base_logical_actions": base_actions,
        "retries": retries,
        "raw_requests": raw_requests,
        "raw_retry_attempts": raw_retry_attempts,
        "expected_amplification_percent": expected_amplification,
    }


def _timing_nodes(value: Any, *, path: str = "summary") -> list[tuple[str, Any]]:
    """Collect timing summaries without treating timing fields as summaries."""

    nodes: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        if "timing" in value:
            nodes.append((f"{path}.timing", value.get("timing")))
        for key, item in value.items():
            if key == "timing":
                continue
            nodes.extend(_timing_nodes(item, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nodes.extend(_timing_nodes(item, path=f"{path}[{index}]"))
    return nodes


def timing_completeness(
    *,
    logical_summary: Any,
    raw_http_summary: Any,
    phase_summaries: Any = None,
    require_metrics: bool = False,
) -> dict[str, Any]:
    """Validate all timing summaries used by one current acceptance report."""

    nodes: list[tuple[str, Any]] = []

    def add_summary(path: str, summary: Any) -> None:
        """Require a timing object whenever a non-empty summary is present."""

        if not isinstance(summary, dict) or not summary:
            nodes.append((f"{path}.timing", None))
            return
        if "timing" not in summary:
            nodes.append((f"{path}.timing", None))
            return
        nodes.append((f"{path}.timing", summary.get("timing")))

    def add_phase_summaries(phases: Any) -> None:
        if not isinstance(phases, dict) or not phases:
            return
        for phase_name, phase in phases.items():
            phase_path = f"phases.{phase_name}"
            if not isinstance(phase, dict):
                nodes.append((f"{phase_path}.timing", None))
                continue
            # A read-mix concurrency ramp is a phase container: each named
            # stage carries its own full-population timing summary.  Requiring
            # a synthetic ``capacity_ramp.timing`` would either reject the
            # producer shape or invite an aggregate substitution, so validate
            # only the explicitly nested stage summaries.
            if isinstance(phase.get("stages"), Mapping):
                for stage_name, stage in phase["stages"].items():
                    add_summary(
                        f"{phase_path}.stages.{stage_name}",
                        stage,
                    )
                continue
            # Canonical phase reports contain separate logical and raw HTTP
            # boundaries.  A missing boundary is itself incomplete evidence.
            if "logical" in phase or "raw_http" in phase:
                add_summary(f"{phase_path}.logical", phase.get("logical"))
                if phase.get("raw_http") is not phase.get("logical"):
                    add_summary(f"{phase_path}.raw_http", phase.get("raw_http"))
            else:
                add_summary(phase_path, phase)

    # Canonical ready-vote reports have distinct logical-action and raw-HTTP
    # summaries.  Read/page reports pass the same summary in both positions;
    # deduplicate by identity so diagnostics remain concise.
    add_summary("logical", logical_summary)
    if raw_http_summary is not logical_summary:
        add_summary("raw_http", raw_http_summary)
    add_phase_summaries(phase_summaries)

    # Preserve diagnostics for any additional nested timing objects supplied
    # by a report while avoiding duplicate primary/phase nodes.
    nested_nodes = _timing_nodes(phase_summaries, path="phases")
    known = {path for path, _timing in nodes}
    nodes.extend((path, timing) for path, timing in nested_nodes if path not in known)

    # A current canonical report must carry timing evidence at its primary
    # boundary.  Capacity reports may use an empty top-level summary and put
    # the complete evidence in their phase summaries.
    if (
        isinstance(logical_summary, dict)
        and not logical_summary
        and isinstance(raw_http_summary, dict)
        and not raw_http_summary
        and isinstance(phase_summaries, dict)
        and phase_summaries
    ):
        # Empty capacity top-level summaries are intentionally represented by
        # their per-rate phase boundaries.
        nodes = [node for node in nodes if not node[0].startswith("logical.") and not node[0].startswith("raw_http.")]
    if not nodes:
        return {
            "complete": False,
            "summary_count": 0,
            "failed": [{"path": "report", "reason": "missing_timing_summary"}],
        }
    failed = [
        {
            "path": path,
            **_timing_summary_details(timing, require_metrics=require_metrics),
        }
        for path, timing in nodes
        if not timing_summary_is_complete(timing, require_metrics=require_metrics)
    ]
    return {
        "complete": not failed,
        "summary_count": len(nodes),
        "failed": failed,
    }


def _phase_budget_checks(
    phase_name: str,
    phase: Any,
    acceptance_contract: dict[str, Any],
    *,
    max_retries: int | None = None,
    canonical: bool = False,
    expected_logical_scope: str | None = None,
) -> dict[str, Any]:
    """Apply the profile budgets to one measured traffic phase.

    Aggregate percentages can hide a phase that shed or retried all of its
    work.  Every non-empty phase therefore needs its own explicit timing,
    user-observed latency, goodput, shedding and retry evidence.
    """

    if not isinstance(phase, dict):
        return {
            "phase": phase_name,
            "passed": False,
            "checks": {"phase_present": False},
        }
    logical = phase.get("logical")
    raw_http = phase.get("raw_http")
    if not isinstance(logical, dict) or not isinstance(raw_http, dict):
        return {
            "phase": phase_name,
            "passed": False,
            "checks": {"logical_and_raw_http_present": False},
        }

    logical_scope = expected_logical_scope or (
        "logical_user_actions"
        if logical.get("scope") == "logical_user_actions"
        else "full_population"
    )
    logical_evidence = normalize_evidence(
        logical,
        expected_scope=logical_scope,
        required=True,
        canonical=canonical,
        require_goodput=True,
        label=f"phase_{phase_name}_logical",
        expected_statuses=_expected_statuses(acceptance_contract),
    )
    raw_evidence = normalize_evidence(
        raw_http,
        expected_scope="full_population",
        required=True,
        canonical=canonical,
        require_goodput=True,
        label=f"phase_{phase_name}_raw_http",
        expected_statuses=_expected_statuses(acceptance_contract),
    )
    logical_timing = logical.get("timing")
    logical_population_count = (
        _nonnegative_int(logical.get("actions"))
        if logical_scope == "logical_user_actions"
        else _nonnegative_int(logical.get("requests"))
    )
    expected_count = (
        _nonnegative_int(logical_timing.get("expected_count"))
        if isinstance(logical_timing, dict)
        else None
    )
    # A configured zero-count duplicate phase is still required as evidence,
    # but has no latency/goodput population to budget.
    if expected_count == 0:
        logical_outcome = _outcome_consistency(logical)
        raw_outcome = _outcome_consistency(raw_http)
        retry_evidence = _retry_reconciliation(
            logical,
            raw_http,
            canonical=canonical,
        )
        logical_reconciliation = _summary_population_reconciliation(logical)
        raw_reconciliation = _summary_population_reconciliation(raw_http)
        logical_timing_key = (
            "logical_actions_match_timing"
            if logical_scope == "logical_user_actions"
            else "raw_requests_match_timing"
        )
        empty_checks = {
            "empty_phase": True,
            "logical_timing_complete": bool(logical_evidence.get("complete")),
            "raw_http_timing_complete": bool(raw_evidence.get("complete")),
            "logical_outcome_consistent": bool(logical_outcome.get("complete")),
            "raw_outcome_consistent": bool(raw_outcome.get("complete")),
            "retry_population_reconciled": bool(retry_evidence.get("complete")),
            "logical_actions_match_timing": bool(
                logical_reconciliation.get(logical_timing_key)
            ),
            "raw_requests_match_timing": bool(raw_reconciliation.get("raw_requests_match_timing")),
            "raw_requests_cover_logical_actions": (
                _nonnegative_int(raw_http.get("requests")) == 0
                and logical_population_count == 0
            ),
        }
        return {
            "phase": phase_name,
            "passed": all(empty_checks.values()),
            "checks": empty_checks,
            "retry_evidence": retry_evidence,
        }

    checks: dict[str, bool] = {
        "logical_timing_complete": bool(logical_evidence.get("complete")),
        "raw_http_timing_complete": bool(raw_evidence.get("complete")),
    }
    logical_outcome = _outcome_consistency(logical)
    raw_outcome = _outcome_consistency(raw_http)
    checks["logical_outcome_consistent"] = bool(logical_outcome.get("complete"))
    checks["raw_outcome_consistent"] = bool(raw_outcome.get("complete"))
    failure_budget = _number(
        acceptance_contract,
        "logical_final_failure_percent",
        maximum=100,
    )
    if phase_name == "duplicate" and failure_budget is None:
        # Stress/spike contracts intentionally omit the normal logical SLO;
        # their shedding budget is still the explicit upper bound for a
        # duplicate phase. A dedicated duplicate budget, when authored, wins.
        failure_budget = _number(
            acceptance_contract,
            "max_duplicate_failure_percent",
            maximum=100,
        )
        if failure_budget is None:
            failure_budget = _number(
                acceptance_contract,
                "max_shed_percent",
                maximum=100,
            )
    if failure_budget is not None:
        checks["logical_final_failure_budget"] = (
            bool(logical_outcome.get("complete"))
            and _number(
                logical_outcome,
                "actual_failure_rate_percent",
                maximum=100,
            ) is not None
            and logical_outcome.get("actual_failure_rate_percent") <= failure_budget
        )
    retry_evidence = _retry_reconciliation(
        logical,
        raw_http,
        canonical=canonical,
    )
    checks["retry_population_reconciled"] = bool(retry_evidence.get("complete"))
    logical_reconciliation = _summary_population_reconciliation(logical)
    raw_reconciliation = _summary_population_reconciliation(raw_http)
    logical_timing_key = (
        "logical_actions_match_timing"
        if logical_scope == "logical_user_actions"
        else "raw_requests_match_timing"
    )
    checks.update(
        {
            "logical_actions_match_timing": bool(
                logical_reconciliation.get(logical_timing_key)
            ),
            "raw_requests_match_timing": bool(
                raw_reconciliation.get("raw_requests_match_timing")
            ),
            "raw_requests_cover_logical_actions": (
                _nonnegative_int(raw_http.get("requests")) is not None
                and logical_population_count is not None
                and _nonnegative_int(raw_http.get("requests"))
                >= logical_population_count
            ),
        }
    )
    if max_retries is not None:
        checks["raw_requests_within_retry_bound"] = (
            _nonnegative_int(raw_http.get("requests")) is not None
            and expected_count is not None
            and _nonnegative_int(raw_http.get("requests")) >= expected_count
            and _nonnegative_int(raw_http.get("requests"))
            <= expected_count * (max_retries + 1)
        )
    accepted_latency = logical.get("accepted_request_latency")
    if not isinstance(accepted_latency, dict):
        accepted_latency = logical.get("latency")
    logical_latency = _observed_latency(logical)
    accepted_budget = acceptance_contract.get("accepted_request_latency")
    logical_budget = acceptance_contract.get("logical_latency")
    for percentile_name in ("p95", "p99"):
        _budget_check(
            checks,
            f"accepted_{percentile_name}",
            _number(accepted_latency, f"{percentile_name}_ms"),
            _number(accepted_budget, f"{percentile_name}_ms"),
        )
        _budget_check(
            checks,
            f"logical_{percentile_name}",
            _number(logical_latency, f"{percentile_name}_ms"),
            _number(logical_budget, f"{percentile_name}_ms"),
        )

    shed_budget = _number(acceptance_contract, "max_shed_percent")
    _budget_check(
        checks,
        "shed_percent",
        _number(raw_http, "temporary_overload_rate_percent"),
        shed_budget,
    )

    retry_budget = _number(acceptance_contract, "max_retry_amplification_percent")
    if retry_budget is not None:
        retry_percent = retry_evidence.get("expected_amplification_percent")
        checks["retry_amplification_percent"] = (
            bool(retry_evidence.get("complete"))
            and isinstance(retry_percent, (int, float))
            and not isinstance(retry_percent, bool)
            and math.isfinite(float(retry_percent))
            and retry_percent <= retry_budget
        )

    minimum_goodput = _number(
        acceptance_contract,
        "minimum_useful_goodput_actions_per_second",
    )
    if minimum_goodput is not None:
        goodput = _number(logical, "successful_goodput_actions_per_second")
        goodput_evidence = logical_evidence.get("goodput") or {}
        goodput = goodput_evidence.get("reported")
        checks["successful_goodput_measured"] = bool(
            goodput_evidence.get("complete")
        )
        checks["minimum_useful_goodput"] = (
            bool(goodput_evidence.get("complete"))
            and goodput is not None
            and goodput >= minimum_goodput
        )

    retry_percent = retry_evidence.get("expected_amplification_percent")
    return {
        "phase": phase_name,
        "passed": all(checks.values()),
        "checks": checks,
        "accepted_request_latency": accepted_latency,
        "user_observed_latency": logical_latency,
        "shed_percent": _number(raw_http, "temporary_overload_rate_percent"),
        "retry_amplification_percent": retry_percent,
        "successful_goodput_actions_per_second": _number(
            logical,
            "successful_goodput_actions_per_second",
        ),
        "goodput_evidence": logical_evidence.get("goodput"),
        "retry_evidence": retry_evidence,
    }


def _phase_action_population_evidence(
    phase_summaries: Any,
    expected_phase_action_counts: Any,
    *,
    expected_logical_scope: str | None,
    max_retries: int | None,
) -> dict[str, bool]:
    """Bind each dispatchable phase to its authored logical population.

    Read-mix and page-load profiles do not have an authored rate-phase list,
    so their primary populations must be carried in the selected profile
    contract explicitly. The same check is useful for Ready Vote aggregate
    phases: an aggregate counter cannot make an empty or shifted phase look
    complete.
    """

    if expected_phase_action_counts is None:
        return {}
    if not isinstance(expected_phase_action_counts, Mapping):
        return {"phase_population_plan_mapping": False}
    actual = phase_summaries if isinstance(phase_summaries, Mapping) else {}
    expected_items = list(expected_phase_action_counts.items())
    expected_names = [str(name) for name, _value in expected_items]
    actual_names = [
        str(name)
        for name in actual
        if str(name) not in {"state", "capacity_ramp"}
    ]
    checks: dict[str, bool] = {
        "phase_population_names_closed": set(actual_names) == set(expected_names),
    }
    retry_limit = _nonnegative_int(max_retries)
    for raw_name, raw_expected_count in expected_items:
        name = str(raw_name)
        expected_count = _nonnegative_int(raw_expected_count)
        phase = actual.get(raw_name)
        if phase is None:
            phase = actual.get(name)
        logical = phase.get("logical") if isinstance(phase, Mapping) else None
        raw_http = phase.get("raw_http") if isinstance(phase, Mapping) else None
        logical = logical if isinstance(logical, Mapping) else None
        raw_http = raw_http if isinstance(raw_http, Mapping) else None
        logical_count = (
            _nonnegative_int(logical.get("actions"))
            if logical is not None and expected_logical_scope == "logical_user_actions"
            else _nonnegative_int(logical.get("requests"))
            if logical is not None
            else None
        )
        logical_timing = logical.get("timing") if logical is not None else None
        raw_count = _nonnegative_int(raw_http.get("requests")) if raw_http is not None else None
        timing_counts_ok = (
            isinstance(logical_timing, Mapping)
            and all(
                _nonnegative_int(logical_timing.get(field)) == expected_count
                for field in ("expected_count", "submitted_count", "completed_count")
            )
        )
        raw_bound_ok = (
            raw_count is not None
            and expected_count is not None
            and raw_count >= expected_count
            and (
                retry_limit is None
                or raw_count <= expected_count * (retry_limit + 1)
            )
        )
        checks[f"phase_population_{name}"] = (
            expected_count is not None
            and logical_count == expected_count
            and timing_counts_ok
            and raw_bound_ok
        )
    return checks


def phase_plan_completeness(
    *,
    phase_summaries: Any,
    expected_phase_plan: Any = None,
    expected_duplicate_count: int | None = None,
    expected_state_read_count: int | None = None,
    acceptance_contract: dict[str, Any] | None = None,
    max_retries: int | None = None,
    canonical: bool = False,
    expected_logical_scope: str | None = None,
    allowed_phase_names: set[str] | None = None,
    expected_phase_action_counts: Mapping[str, Any] | None = None,
    expected_stage_action_counts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind measured phases to the selected profile's authored plan.

    ``None`` means the caller is using the intentionally retained legacy
    evaluator and has no authored plan to bind.  Canonical callers pass a list
    (including an empty list) and therefore receive strict name/order and
    duplicate-phase validation.
    """

    enforce_plan = expected_phase_plan is not None
    duplicate_count = _nonnegative_int(expected_duplicate_count)
    state_count = _nonnegative_int(expected_state_read_count)
    retry_limit = _nonnegative_int(max_retries)
    enforce_duplicate = expected_duplicate_count is not None
    enforce_state = expected_state_read_count is not None
    enforce_auxiliary = expected_stage_action_counts is not None
    if (
        not enforce_plan
        and not enforce_duplicate
        and not enforce_state
        and not enforce_auxiliary
    ):
        actual = phase_summaries if isinstance(phase_summaries, dict) else {}
        checks = {}
        if canonical:
            checks["phase_names_closed"] = (
                allowed_phase_names is not None
                and set(str(name) for name in actual) == set(allowed_phase_names)
            )
        phase_budgets: dict[str, Any] = {}
        if canonical and actual:
            for name, phase in actual.items():
                if str(name) in {"state", "capacity_ramp"}:
                    continue
                phase_budgets[str(name)] = _phase_budget_checks(
                    str(name),
                    phase,
                    acceptance_contract or {},
                    max_retries=retry_limit,
                    canonical=True,
                    expected_logical_scope=expected_logical_scope,
                )
        checks.update(
            _phase_action_population_evidence(
                actual,
                expected_phase_action_counts,
                expected_logical_scope=expected_logical_scope,
                max_retries=retry_limit,
            )
        )
        phase_budgets_ok = all(
            isinstance(result, Mapping) and result.get("passed") is True
            for result in phase_budgets.values()
        )
        return {
            "complete": all(checks.values()) and phase_budgets_ok,
            "checks": checks,
            "expected_phases": None,
            "actual_phases": list(actual),
            "phase_budgets": phase_budgets,
            "phase_population": {
                key: value
                for key, value in checks.items()
                if key.startswith("phase_population_")
            },
        }

    actual = phase_summaries if isinstance(phase_summaries, dict) else {}
    checks: dict[str, bool] = {}
    if canonical:
        checks["phase_names_closed"] = (
            allowed_phase_names is not None
            and set(str(name) for name in actual) == set(allowed_phase_names)
        )
    expected_auxiliary_names = {"capacity_ramp"} if enforce_auxiliary else set()
    actual_auxiliary_names = {
        str(name) for name in actual if str(name) == "capacity_ramp"
    }
    checks["auxiliary_phase_names_closed"] = (
        actual_auxiliary_names == expected_auxiliary_names
    )
    if enforce_duplicate:
        checks["expected_duplicate_count_typed"] = duplicate_count is not None
    if enforce_state:
        checks["expected_state_read_count_typed"] = state_count is not None
    if max_retries is not None:
        checks["max_retries_typed"] = retry_limit is not None
    expected_phases: list[dict[str, Any]] = []
    if enforce_plan:
        if isinstance(expected_phase_plan, (list, tuple)):
            expected_phases = [
                item for item in expected_phase_plan if isinstance(item, dict)
            ]
        expected_names = [str(item.get("name")) for item in expected_phases]
        actual_names = [
            str(name) for name in actual
            if str(name) not in {"primary", "duplicate", "state", "capacity_ramp"}
        ]
        checks["phase_names_and_order"] = (
            isinstance(expected_phase_plan, (list, tuple))
            and len(expected_phases) == len(expected_phase_plan)
            and actual_names == expected_names
        ) if isinstance(expected_phase_plan, (list, tuple)) else False
        if not enforce_duplicate:
            checks["unexpected_duplicate_phase"] = "duplicate" not in actual
        if not enforce_state:
            checks["unexpected_state_phase"] = "state" not in actual

        for expected in expected_phases:
            name = str(expected.get("name"))
            phase = actual.get(name)
            phase_ok = isinstance(phase, dict)
            if phase_ok:
                phase_mapping = phase
                # Read-mix and page-load profiles do not author target rates
                # or durations.  Their derived plan still binds exact phase
                # names and populations below, but must not invent metadata
                # that the producer never measured.  Authored Ready Vote
                # phases retain the full configured/submitted/complete gate.
                phase_metadata_required = any(
                    key in expected
                    for key in (
                        "target_logical_actions_per_second",
                        "duration_seconds",
                        "configured_actions",
                        "submitted_actions",
                        "missing_actions",
                        "complete",
                    )
                )
                phase_ok = (
                    _numeric_equal(
                        phase_mapping.get("target_logical_actions_per_second"),
                        expected.get("target_logical_actions_per_second"),
                    )
                    and _numeric_equal(
                        phase_mapping.get("duration_seconds"),
                        expected.get("duration_seconds"),
                    )
                )
                logical = phase_mapping.get("logical")
                timing = logical.get("timing") if isinstance(logical, dict) else None
                expected_actions = _nonnegative_int(expected.get("logical_actions"))
                logical_population_count = (
                    _nonnegative_int(logical.get("actions"))
                    if expected_logical_scope == "logical_user_actions"
                    else _nonnegative_int(logical.get("requests"))
                    if isinstance(logical, Mapping)
                    else None
                )
                phase_population = (
                    isinstance(logical, dict)
                    and logical_population_count == expected_actions
                    and _nonnegative_int(timing.get("expected_count")) == expected_actions
                    and _nonnegative_int(timing.get("submitted_count")) == expected_actions
                    and _nonnegative_int(timing.get("completed_count")) == expected_actions
                    and timing_summary_is_complete(timing, require_metrics=canonical)
                )
                if phase_metadata_required:
                    phase_population = phase_population and (
                        _nonnegative_int(phase_mapping.get("configured_actions"))
                        == expected_actions
                        and _nonnegative_int(phase_mapping.get("submitted_actions"))
                        == expected_actions
                        and _nonnegative_int(phase_mapping.get("missing_actions")) == 0
                        and phase_mapping.get("complete") is True
                    )
                phase_ok = phase_ok and phase_population
                if isinstance(logical, dict):
                    phase_ok = phase_ok and _numeric_equal(
                        logical.get("target_logical_actions_per_second"),
                        expected.get("target_logical_actions_per_second"),
                    )
                raw_http = phase_mapping.get("raw_http")
                raw_reconciliation = _summary_population_reconciliation(raw_http)
                raw_requests = (
                    _nonnegative_int(raw_http.get("requests"))
                    if isinstance(raw_http, dict)
                    else None
                )
                raw_bounds_ok = (
                    raw_requests is not None
                    and expected_actions is not None
                    and raw_requests >= expected_actions
                    and (
                        retry_limit is None
                        or raw_requests <= expected_actions * (retry_limit + 1)
                    )
                )
                phase_ok = phase_ok and bool(
                    isinstance(raw_http, dict)
                    and raw_reconciliation.get("raw_requests_match_timing")
                    and raw_bounds_ok
                )
            checks[f"phase_{name}"] = phase_ok

    if enforce_duplicate:
        duplicate = actual.get("duplicate")
        duplicate_ok = isinstance(duplicate, dict)
        if duplicate_ok:
            duplicate_mapping = duplicate
            primary_mapping = actual.get("primary")
            primary_logical = (
                primary_mapping.get("logical")
                if isinstance(primary_mapping, Mapping)
                else None
            )
            primary_successes = (
                _nonnegative_int(primary_logical.get("final_successes"))
                if isinstance(primary_logical, Mapping)
                else None
            )
            candidate_actions = _nonnegative_int(
                duplicate_mapping.get("candidate_actions")
            )
            duplicate_ok = (
                _nonnegative_int(duplicate_mapping.get("configured_actions"))
                == duplicate_count
                and _nonnegative_int(duplicate_mapping.get("submitted_actions"))
                == duplicate_count
                and _nonnegative_int(duplicate_mapping.get("completed_actions"))
                == duplicate_count
                and _nonnegative_int(duplicate_mapping.get("missing_actions")) == 0
                and duplicate_mapping.get("complete") is True
                and candidate_actions is not None
                and primary_successes is not None
                and candidate_actions == primary_successes
                and candidate_actions >= duplicate_count
            )
            logical = duplicate_mapping.get("logical")
            raw_http = duplicate_mapping.get("raw_http")
            timing = logical.get("timing") if isinstance(logical, dict) else None
            final_successes = (
                _nonnegative_int(logical.get("final_successes"))
                if isinstance(logical, dict)
                else None
            )
            final_failures = (
                _nonnegative_int(logical.get("final_failures"))
                if isinstance(logical, dict)
                else None
            )
            duplicate_ok = duplicate_ok and (
                isinstance(logical, dict)
                and _nonnegative_int(logical.get("actions")) == duplicate_count
                and _nonnegative_int(timing.get("expected_count")) == duplicate_count
                and _nonnegative_int(timing.get("submitted_count")) == duplicate_count
                and _nonnegative_int(timing.get("completed_count")) == duplicate_count
                and final_successes is not None
                and final_failures is not None
                and final_successes + final_failures == duplicate_count
                and timing_summary_is_complete(timing, require_metrics=canonical)
            )
            duplicate_ok = duplicate_ok and isinstance(raw_http, dict) and (
                _nonnegative_int(raw_http.get("configured_logical_actions"))
                == duplicate_count
                and _nonnegative_int(raw_http.get("submitted_logical_actions"))
                == duplicate_count
                and _nonnegative_int(raw_http.get("missing_logical_actions")) == 0
                and timing_summary_is_complete(
                    raw_http.get("timing"),
                    require_metrics=canonical,
                )
            )
            raw_requests = (
                _nonnegative_int(raw_http.get("requests"))
                if isinstance(raw_http, dict)
                else None
            )
            duplicate_ok = duplicate_ok and (
                raw_requests is not None
                and raw_requests >= duplicate_count
                and (
                    retry_limit is None
                    or raw_requests <= duplicate_count * (retry_limit + 1)
                )
            )
            duplicate_idempotency = _duplicate_idempotency_checks(
                duplicate,
                duplicate_count,
            )
            duplicate_ok = duplicate_ok and bool(duplicate_idempotency["complete"])
            # User identity digests are deliberately not retained in load
            # evidence.  Exact population binding remains count/timing/state
            # based above; retaining a digest would turn synthetic user IDs
            # into an unnecessary correlation identifier.
        checks["duplicate_population"] = duplicate_ok

    state_evidence: dict[str, Any] = {}
    if enforce_state:
        primary_mapping = actual.get("primary")
        primary_logical = (
            primary_mapping.get("logical")
            if isinstance(primary_mapping, Mapping)
            else None
        )
        primary_successes = (
            _nonnegative_int(primary_logical.get("final_successes"))
            if isinstance(primary_logical, Mapping)
            else None
        )
        state_evidence = _state_read_evidence(
            actual.get("state"),
            state_count,
            canonical=canonical,
            expected_primary_successes=primary_successes,
            expected_statuses=_expected_statuses(acceptance_contract),
        )
        checks["state_read_population"] = bool(state_evidence["complete"])

    phase_budgets: dict[str, Any] = {}
    if enforce_plan or enforce_duplicate:
        for name, phase in actual.items():
            if str(name) in {"state", "capacity_ramp"}:
                continue
            phase_budgets[str(name)] = _phase_budget_checks(
                str(name),
                phase,
                acceptance_contract or {},
                max_retries=retry_limit,
                canonical=canonical,
                expected_logical_scope=expected_logical_scope,
            )
    checks.update(
        _phase_action_population_evidence(
            actual,
            expected_phase_action_counts,
            expected_logical_scope=expected_logical_scope,
            max_retries=retry_limit,
        )
    )
    return {
        "complete": all(checks.values()),
        "checks": checks,
        "expected_phases": [str(item.get("name")) for item in expected_phases]
        if enforce_plan
        else None,
        "actual_phases": [str(name) for name in actual],
        "phase_budgets": phase_budgets,
        "phase_population": {
            key: value
            for key, value in checks.items()
            if key.startswith("phase_population_")
        },
        "state_evidence": state_evidence,
    }


def _origin_safety_checks(
    contract: dict[str, Any],
    origin_observability: dict[str, Any],
    *,
    require_complete_diagnostics: bool = False,
) -> dict[str, Any]:
    """Evaluate bounded origin evidence without treating CPU as saturation alone."""

    if not isinstance(origin_observability, Mapping):
        origin_observability = {}

    def mapping_or_empty(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    checks: dict[str, bool] = {}
    system = mapping_or_empty(origin_observability.get("system"))
    server = mapping_or_empty(origin_observability.get("server_request_perf_logs"))
    pool = mapping_or_empty(server.get("pool_checkout_wait_ms"))
    waits = mapping_or_empty(system.get("postgres_waits"))
    postgres_backends = mapping_or_empty(system.get("postgres_backend_connections"))
    cpu_per_core = mapping_or_empty(system.get("cpu_per_core"))
    missing_diagnostics: list[str] = []

    pool_contract = contract.get("pool_checkout_wait_ms") or {}
    for percentile_name in ("p95", "p99"):
        actual = _number(pool, f"{percentile_name}_ms")
        budget = _number(pool_contract, f"{percentile_name}_ms")
        if budget is None:
            continue
        if actual is None:
            missing_diagnostics.append(f"pool_checkout_{percentile_name}_ms")
        else:
            checks[f"pool_checkout_{percentile_name}_ms"] = actual <= budget
    _budget_check(
        checks,
        "postgres_backend_connections",
        _number(postgres_backends, "max"),
        _number(contract, "max_postgres_backend_connections"),
    )
    ownership_consistency = system.get("postgres_backend_ownership_consistency")
    if isinstance(ownership_consistency, Mapping):
        checks["postgres_backend_ownership_consistent"] = (
            ownership_consistency.get("all_match") is True
        )
    _budget_check(
        checks,
        "waiting_backends",
        _number(waits, "max_waiting_backends"),
        _number(contract, "max_waiting_backends"),
    )
    _budget_check(
        checks,
        "lock_waiters",
        _number(waits, "max_lock_waiters"),
        _number(contract, "max_lock_waiters"),
    )

    max_cpu = _number(contract, "max_cpu_per_core_percent")
    if max_cpu is not None:
        cpu_values = [
            _number(value, "max_percent")
            for value in cpu_per_core.values()
            if isinstance(value, dict)
        ]
        checks["cpu_per_core"] = bool(cpu_values) and max(cpu_values) <= max_cpu

    required = (
        origin_observability.get("stop_file_seen") is True
        and origin_observability.get("timed_out") is False
    )
    checks["observer_completed"] = required
    if require_complete_diagnostics:
        # A missing bounded sample is not evidence that the resource stayed
        # within budget.  Keep the missing field in the report for forensics,
        # but fail the acceptance decision closed.
        checks["required_diagnostics_present"] = not missing_diagnostics
    return {
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
        "evidence_scope": "origin_observer_summary",
        "missing_diagnostics": missing_diagnostics,
        "postgres_backend_connections": postgres_backends,
        "postgres_tcp_established_connections": (
            system.get("postgres_tcp_established_connections") or {}
        ),
    }


def _observer_binding_check(
    origin_observability: dict[str, Any],
    *,
    expected_fixture_marker: str | None,
    expected_external_run_id: str | None,
) -> dict[str, Any]:
    if not isinstance(origin_observability, Mapping):
        origin_observability = {}
    binding = origin_observability.get("binding")
    binding = binding if isinstance(binding, dict) else {}
    actual_marker = binding.get("fixture_marker")
    actual_run_id = binding.get("external_run_id")
    marker_ok = (
        isinstance(expected_fixture_marker, str)
        and _FIXTURE_MARKER_RE.fullmatch(expected_fixture_marker) is not None
        and isinstance(actual_marker, str)
        and _FIXTURE_MARKER_RE.fullmatch(actual_marker) is not None
        and actual_marker == expected_fixture_marker
    )
    run_id_ok = (
        isinstance(expected_external_run_id, str)
        and isinstance(actual_run_id, str)
        and _EXTERNAL_RUN_ID_RE.fullmatch(expected_external_run_id) is not None
        and _EXTERNAL_RUN_ID_RE.fullmatch(actual_run_id) is not None
        and actual_run_id == expected_external_run_id
    )
    observer_complete = (
        binding.get("complete") is True
        and origin_observability.get("stop_file_seen") is True
        and origin_observability.get("timed_out") is False
    )
    complete = observer_complete and marker_ok and run_id_ok
    return {
        "complete": complete,
        "fixture_marker": actual_marker,
        "external_run_id": actual_run_id,
        "expected_fixture_marker": expected_fixture_marker,
        "expected_external_run_id": expected_external_run_id,
        "checks": {
            "fixture_marker": marker_ok,
            "external_run_id": run_id_ok,
            "binding_complete": binding.get("complete") is True,
            "observer_stop_file_seen": origin_observability.get("stop_file_seen") is True,
            "observer_not_timed_out": origin_observability.get("timed_out") is False,
        },
    }


def observer_evidence_required(
    acceptance_contract: dict[str, Any] | None,
    *,
    require_exact_observer_binding: bool,
    canonical_evidence: bool = False,
) -> bool:
    """Return whether a canonical result must include origin observation.

    Exact fixture/run binding is meaningless without an observer report to
    bind.  Treat that execution-level requirement as an implicit origin
    evidence requirement even when an older profile omitted the redundant
    ``acceptance.require_origin_evidence`` flag.
    """

    return bool(canonical_evidence) or bool(require_exact_observer_binding) or bool(
        isinstance(acceptance_contract, dict)
        and acceptance_contract.get("require_origin_evidence") is True
    )


def evaluate_acceptance(
    *,
    contract_ok: bool,
    logical_summary: dict[str, Any],
    p95_budget_ms: float | None = None,
    p99_budget_ms: float | None = None,
    final_failure_budget_percent: float | None = None,
    raw_http_summary: dict[str, Any] | None = None,
    acceptance_contract: dict[str, Any] | None = None,
    origin_observability: dict[str, Any] | None = None,
    phase_summaries: dict[str, Any] | None = None,
    canonical_evidence: bool = False,
    expected_logical_scope: str | None = None,
    allowed_phase_names: set[str] | None = None,
    expected_stage_action_counts: Mapping[str, Any] | None = None,
    expected_phase_action_counts: Mapping[str, Any] | None = None,
    expected_fixture_marker: str | None = None,
    expected_external_run_id: str | None = None,
    require_exact_observer_binding: bool = False,
    expected_phase_plan: Any = None,
    expected_duplicate_count: int | None = None,
    expected_primary_action_count: int | None = None,
    expected_total_logical_action_count: int | None = None,
    expected_state_read_count: int | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """Return a versioned SLO, capacity, spike or stress decision.

    The first three optional budget arguments remain for compatibility with
    the original runner and its focused unit tests.  Canonical profiles pass
    ``acceptance_contract`` instead.
    """

    logical_summary = logical_summary if isinstance(logical_summary, Mapping) else {}
    raw_http_summary = (
        raw_http_summary if isinstance(raw_http_summary, Mapping) else {}
    )

    if acceptance_contract is not None and not isinstance(acceptance_contract, Mapping):
        # A malformed selected contract is not a legacy-compatible empty
        # contract.  Return a structured fail-closed result rather than
        # allowing a type error to escape or a producer flag to decide.
        return {
            "passed": False,
            "decision": "ACCEPTANCE CONTRACT FAIL",
            "authoritative": False,
            "dispatchable": False,
            "contract_ok": False,
            "checks": {"acceptance_contract_mapping": False},
            "pending_origin_evidence": False,
        }

    observed_latency = _observed_latency(logical_summary)
    # ``end_to_end_latency`` is service/executor timing and cannot stand in
    # for the user-observed schedule-to-completion interval.
    latency = observed_latency or {}
    accepted_latency = logical_summary.get("accepted_request_latency", {})
    if not accepted_latency:
        accepted_latency = logical_summary.get("latency", {})
    logical_p95_value = _number(latency, "p95_ms")
    logical_p99_value = _number(latency, "p99_ms")
    logical_p95 = logical_p95_value or 0.0
    logical_p99 = logical_p99_value or 0.0
    final_failure_rate = _number(logical_summary, "final_failure_rate_percent") or 0.0
    raw_http_summary = raw_http_summary or {}
    shed_percent = _number(raw_http_summary, "temporary_overload_rate_percent") or 0.0
    actions = _number(logical_summary, "actions") or _number(raw_http_summary, "requests") or 0.0
    retries = _number(logical_summary, "total_retries") or 0.0
    retry_amplification_percent = retries * 100 / max(1.0, actions)
    goodput = _successful_goodput(logical_summary, raw_http_summary)

    # ``acceptance_contract`` is present for every current canonical profile.
    # Its timing evidence is checked below for every profile kind.
    timing_evidence = timing_completeness(
        logical_summary=logical_summary,
        raw_http_summary=raw_http_summary,
        phase_summaries=phase_summaries,
        require_metrics=canonical_evidence,
    )
    stage_evidence = _capacity_ramp_evidence(
        (phase_summaries or {}).get("capacity_ramp")
        if isinstance(phase_summaries, Mapping)
        else None,
        expected_stage_action_counts,
        canonical=canonical_evidence,
        expected_statuses=_expected_statuses(acceptance_contract),
        acceptance_contract=acceptance_contract,
        max_retries=max_retries,
    )
    observer_required = observer_evidence_required(
        acceptance_contract,
        require_exact_observer_binding=require_exact_observer_binding,
        canonical_evidence=canonical_evidence,
    )
    budget_evidence = _acceptance_budget_evidence(
        acceptance_contract,
        require_statuses=canonical_evidence,
    )

    if acceptance_contract is None:
        # The direct platform_external_load.py entry point is retained as a
        # diagnostic implementation detail.  It has no profile/source/run
        # binding and must never produce an acceptance PASS.
        return {
            "passed": False,
            "decision": "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
            "authoritative": False,
            "dispatchable": False,
            "contract_ok": False,
            "checks": {
                "acceptance_contract_present": False,
                "timing_complete": bool(timing_evidence["complete"]),
                "capacity_ramp_evidence": bool(stage_evidence["complete"]),
                "acceptance_budget_evidence": bool(budget_evidence["complete"]),
            },
            "p95_budget_ms": p95_budget_ms,
            "p99_budget_ms": p99_budget_ms,
            "p95_ms": logical_p95,
            "p99_ms": logical_p99,
            "accepted_request_p95_ms": _number(accepted_latency, "p95_ms") or 0.0,
            "accepted_request_p99_ms": _number(accepted_latency, "p99_ms") or 0.0,
            "logical_final_failure_rate_percent": final_failure_rate,
            "pending_origin_evidence": False,
            "observer_binding": None,
            "timing_evidence": timing_evidence,
        }

    kind = str(acceptance_contract.get("kind") or "slo")
    inferred_logical_scope = expected_logical_scope or (
        "logical_user_actions"
        if expected_duplicate_count is not None
        or expected_state_read_count is not None
        else "full_population"
    )
    normalized_logical = normalize_evidence(
        logical_summary,
        expected_scope=inferred_logical_scope,
        required=kind != "capacity" or bool(logical_summary),
        canonical=canonical_evidence,
        require_goodput=True,
        label="logical",
        expected_statuses=_expected_statuses(acceptance_contract),
    )
    normalized_raw = normalize_evidence(
        raw_http_summary,
        expected_scope="full_population",
        required=kind != "capacity" or bool(raw_http_summary),
        canonical=canonical_evidence,
        require_goodput=True,
        label="raw_http",
        expected_statuses=_expected_statuses(acceptance_contract),
    )
    if canonical_evidence:
        # The normalized records are the only source for canonical totals;
        # never infer raw counts from the logical layer (or vice versa).
        goodput = (normalized_logical.get("goodput") or {}).get("reported")
    logical_outcome = _outcome_consistency(
        logical_summary,
        required=kind != "capacity" or bool(logical_summary),
    )
    raw_outcome = _outcome_consistency(
        raw_http_summary,
        required=kind != "capacity" or bool(raw_http_summary),
    )
    if logical_outcome.get("kind") == "logical":
        final_failure_rate = float(
            logical_outcome.get("actual_failure_rate_percent") or 0.0
        )
    elif raw_outcome.get("kind") == "raw_http":
        final_failure_rate = float(
            raw_outcome.get("actual_failure_rate_percent") or 0.0
        )
    outcome_checks = {
        "logical_outcome_consistency": bool(logical_outcome.get("complete"))
        and (not canonical_evidence or bool(normalized_logical.get("complete"))),
        "raw_outcome_consistency": bool(raw_outcome.get("complete"))
        and (not canonical_evidence or bool(normalized_raw.get("complete"))),
    }
    retry_evidence = _retry_reconciliation(
        logical_summary,
        raw_http_summary,
        canonical=canonical_evidence and bool(logical_summary or raw_http_summary),
    )
    if canonical_evidence and (logical_summary or raw_http_summary):
        # Use the reconciled base population for amplification.  A full HTTP
        # envelope includes retry attempts, so dividing by ``requests`` would
        # understate retries and could let an over-retried run pass.
        derived_retry_amplification = retry_evidence.get(
            "expected_amplification_percent"
        )
        retry_amplification_percent = (
            float(derived_retry_amplification)
            if isinstance(derived_retry_amplification, (int, float))
            and not isinstance(derived_retry_amplification, bool)
            and math.isfinite(float(derived_retry_amplification))
            and derived_retry_amplification >= 0
            else MAX_EVIDENCE_NUMBER
        )
    outcome_checks["retry_population_reconciled"] = bool(
        retry_evidence.get("complete")
    )
    phase_retry_evidence = _phase_retry_totals_reconciliation(
        phase_summaries,
        logical_summary,
        raw_http_summary,
        canonical=canonical_evidence,
    )
    if phase_retry_evidence:
        outcome_checks["phase_retry_totals_reconciled"] = bool(
            phase_retry_evidence.get("complete")
        )
    phase_budget_contract = acceptance_contract
    if kind == "capacity" and isinstance(acceptance_contract.get("slo"), dict):
        phase_budget_contract = dict(acceptance_contract["slo"])
        if "minimum_useful_goodput_actions_per_second" in acceptance_contract:
            phase_budget_contract[
                "minimum_useful_goodput_actions_per_second"
            ] = acceptance_contract["minimum_useful_goodput_actions_per_second"]
        if "expected_statuses" in acceptance_contract:
            phase_budget_contract["expected_statuses"] = acceptance_contract[
                "expected_statuses"
            ]
    phase_plan_evidence = phase_plan_completeness(
        phase_summaries=phase_summaries,
        expected_phase_plan=expected_phase_plan,
        expected_duplicate_count=expected_duplicate_count,
        expected_state_read_count=expected_state_read_count,
        acceptance_contract=phase_budget_contract,
        max_retries=max_retries,
        canonical=canonical_evidence,
        expected_logical_scope=inferred_logical_scope,
        allowed_phase_names=allowed_phase_names,
        expected_phase_action_counts=expected_phase_action_counts,
        expected_stage_action_counts=expected_stage_action_counts,
    )
    phase_population_checks = _phase_population_reconciliation(
        phase_summaries,
        logical_summary,
        raw_http_summary,
    )
    raw_logical_population_checks = _raw_logical_population_reconciliation(
        logical_summary,
        raw_http_summary,
        max_retries=max_retries,
    )
    top_population_timing_checks = _top_population_timing_checks(
        logical_summary,
        raw_http_summary,
    )
    enforce_phase_contract = (
        expected_phase_plan is not None
        or expected_duplicate_count is not None
        or expected_phase_action_counts is not None
    )
    phase_budget_results = phase_plan_evidence.get("phase_budgets") or {}
    phase_budgets_ok = all(
        isinstance(result, dict) and result.get("passed") is True
        for result in phase_budget_results.values()
    )
    require_phase_budgets = enforce_phase_contract or (
        canonical_evidence and bool(phase_budget_results)
    )
    population_checks: dict[str, bool] = {}
    if (
        expected_primary_action_count is not None
        or expected_total_logical_action_count is not None
        or expected_state_read_count is not None
    ):
        expected_primary = _nonnegative_int(expected_primary_action_count)
        expected_duplicate = (
            _nonnegative_int(expected_duplicate_count)
            if expected_duplicate_count is not None
            else 0
        )
        expected_total = (
            _nonnegative_int(expected_total_logical_action_count)
            if expected_total_logical_action_count is not None
            else (
                expected_primary + expected_duplicate
                if expected_primary is not None and expected_duplicate is not None
                else None
            )
        )
        logical_timing = logical_summary.get("timing")
        logical_kind = logical_outcome.get("kind")
        logical_count = (
            _nonnegative_int(logical_summary.get("actions"))
            if logical_kind == "logical"
            else _nonnegative_int(logical_summary.get("requests"))
        )
        total_timing_ok = (
            expected_total is not None
            and isinstance(logical_timing, dict)
            and _nonnegative_int(logical_timing.get("expected_count")) == expected_total
            and _nonnegative_int(logical_timing.get("submitted_count")) == expected_total
            and _nonnegative_int(logical_timing.get("completed_count")) == expected_total
        )
        population_checks = {
            "primary_population": (
                expected_primary is not None
                and _nonnegative_int(logical_summary.get("primary_actions"))
                == expected_primary
            ),
            "logical_population": (
                expected_total is not None
                and logical_count == expected_total
                and total_timing_ok
            ),
        }
        if expected_duplicate_count is not None:
            population_checks["duplicate_aggregate"] = (
                _nonnegative_int(logical_summary.get("duplicate_actions"))
                == expected_duplicate
                and _nonnegative_int(
                    logical_summary.get("configured_duplicate_actions")
                )
                == expected_duplicate
            )
        if expected_state_read_count is not None:
            expected_state = _nonnegative_int(expected_state_read_count)
            raw_state_reads = _nonnegative_int(
                raw_http_summary.get("state_read_requests")
            )
            raw_total_requests = _nonnegative_int(
                raw_http_summary.get("total_requests_including_state")
            )
            raw_requests = _nonnegative_int(raw_http_summary.get("requests"))
            population_checks.update(
                {
                    "state_read_requests": (
                        expected_state is not None
                        and raw_state_reads == expected_state
                    ),
                    "state_requests_reconcile_to_raw_total": (
                        expected_state is not None
                        and raw_requests is not None
                        and raw_total_requests is not None
                        and raw_total_requests == raw_requests + expected_state
                    ),
                }
            )
            population_checks["state_read_population"] = bool(
                phase_plan_evidence.get("checks", {}).get("state_read_population")
            )
    if kind in {"stress", "spike"}:
        checks = {
            "contract": contract_ok is True,
            "unexpected_statuses": _nonnegative_int(
                raw_http_summary.get("unexpected_statuses")
            )
            == 0,
            "goodput_positive": goodput is not None and goodput > 0.0,
            "timing_complete": bool(timing_evidence["complete"]),
            "capacity_ramp_evidence": bool(stage_evidence["complete"]),
            "acceptance_budget_evidence": bool(budget_evidence["complete"]),
            **outcome_checks,
            **phase_population_checks,
            **raw_logical_population_checks,
            **top_population_timing_checks,
            **population_checks,
        }
        minimum_goodput = _number(
            acceptance_contract,
            "minimum_useful_goodput_actions_per_second",
        )
        if minimum_goodput is not None:
            checks["successful_goodput_measured"] = goodput is not None
            checks["minimum_useful_goodput"] = (
                goodput is not None and goodput >= minimum_goodput
            )
        _budget_check(
            checks,
            "accepted_p95_ms",
            _number(accepted_latency, "p95_ms"),
            _number(acceptance_contract.get("accepted_request_latency"), "p95_ms"),
        )
        _budget_check(
            checks,
            "accepted_p99_ms",
            _number(accepted_latency, "p99_ms"),
            _number(acceptance_contract.get("accepted_request_latency"), "p99_ms"),
        )
        _budget_check(
            checks,
            "logical_p95_ms",
            logical_p95_value,
            _number(acceptance_contract.get("logical_latency"), "p95_ms"),
        )
        _budget_check(
            checks,
            "logical_p99_ms",
            logical_p99_value,
            _number(acceptance_contract.get("logical_latency"), "p99_ms"),
        )
        _budget_check(
            checks,
            "retry_amplification_percent",
            retry_amplification_percent,
            _number(acceptance_contract, "max_retry_amplification_percent"),
        )
        _budget_check(
            checks,
            "shed_percent",
            shed_percent,
            _number(acceptance_contract, "max_shed_percent"),
        )
        spike_metrics: dict[str, Any] = {}
        if kind == "spike":
            recovery = acceptance_contract.get("recovery") or {}
            burst_name = str(recovery.get("burst_phase") or "")
            recovery_name = str(recovery.get("recovery_phase") or "")
            burst = (phase_summaries or {}).get(burst_name)
            recovered = (phase_summaries or {}).get(recovery_name)
            checks["spike_phases_present"] = isinstance(burst, dict) and isinstance(recovered, dict)
            checks["phase_completion"] = bool(phase_summaries) and all(
                isinstance(phase, dict)
                and timing_completeness(
                    logical_summary=phase.get("logical"),
                    raw_http_summary=phase.get("raw_http"),
                    require_metrics=canonical_evidence,
                )["complete"]
                for name, phase in (phase_summaries or {}).items()
                if str(name) not in {"state", "capacity_ramp"}
            )
            if isinstance(burst, dict) and isinstance(recovered, dict):
                burst_logical = burst.get("logical") or {}
                recovered_logical = recovered.get("logical") or {}
                burst_raw = burst.get("raw_http") or {}
                recovered_raw = recovered.get("raw_http") or {}
                spike_metrics = {
                    "burst": {
                        "accepted_request_latency": burst_logical.get("accepted_request_latency") or burst_logical.get("latency"),
                        "logical_latency": _observed_latency(burst_logical),
                        "shed_percent": burst_raw.get("temporary_overload_rate_percent", 0),
                        "goodput": burst_logical.get("successful_goodput_actions_per_second"),
                    },
                    "recovery": {
                        "accepted_request_latency": recovered_logical.get("accepted_request_latency") or recovered_logical.get("latency"),
                        "logical_latency": _observed_latency(recovered_logical),
                        "shed_percent": recovered_raw.get("temporary_overload_rate_percent", 0),
                        "goodput": recovered_logical.get("successful_goodput_actions_per_second"),
                    },
                }
                checks["recovery_goodput_positive"] = (
                    isinstance(spike_metrics["recovery"]["goodput"], (int, float))
                    and not isinstance(spike_metrics["recovery"]["goodput"], bool)
                    and float(spike_metrics["recovery"]["goodput"]) > 0
                )
        if require_phase_budgets:
            checks["phase_plan"] = bool(phase_plan_evidence["complete"])
            checks["phase_budgets"] = phase_budgets_ok
        binding = None
        if require_exact_observer_binding or canonical_evidence:
            binding = _observer_binding_check(
                origin_observability or {},
                expected_fixture_marker=expected_fixture_marker,
                expected_external_run_id=expected_external_run_id,
            )
            checks["observer_binding"] = bool(binding["complete"])
        if origin_observability is None:
            return {
                "passed": False,
                "decision": "SPIKE PENDING ORIGIN EVIDENCE" if kind == "spike" else "STRESS PENDING ORIGIN EVIDENCE",
                "pending_origin_evidence": observer_required,
                "contract_ok": contract_ok is True,
                "checks": checks,
                "shed_percent": shed_percent,
                "retry_amplification_percent": round(retry_amplification_percent, 4),
                "accepted_request_p95_ms": _number(accepted_latency, "p95_ms") or 0.0,
                "accepted_request_p99_ms": _number(accepted_latency, "p99_ms") or 0.0,
                "logical_p95_ms": logical_p95,
                "logical_p99_ms": logical_p99,
                "logical_final_failure_rate_percent": final_failure_rate,
                "outcome_evidence": {
                    "logical": logical_outcome,
                    "raw_http": raw_outcome,
                },
                "spike_metrics": spike_metrics,
                "observer_binding": binding,
                "timing_evidence": timing_evidence,
                "phase_plan_evidence": phase_plan_evidence,
                "phase_population_evidence": phase_population_checks,
                "raw_logical_population_evidence": raw_logical_population_checks,
                "top_population_timing_evidence": top_population_timing_checks,
                "phase_budget_evidence": phase_budget_results,
                "phase_retry_evidence": phase_retry_evidence,
            }
        origin = _origin_safety_checks(
            acceptance_contract,
            origin_observability,
            require_complete_diagnostics=(
                require_exact_observer_binding or canonical_evidence
            ),
        )
        checks["origin_safety"] = bool(origin["passed"])
        passed = all(checks.values())
        return {
            "passed": passed,
            "decision": (
                ("SPIKE BEHAVIOR PASS" if passed else "SPIKE BEHAVIOR FAIL")
                if kind == "spike"
                else ("STRESS BEHAVIOR PASS" if passed else "STRESS BEHAVIOR FAIL")
            ),
            "pending_origin_evidence": False,
            "contract_ok": contract_ok is True,
            "checks": checks,
            "origin_safety": origin,
            "observer_binding": binding,
            "shed_percent": shed_percent,
            "retry_amplification_percent": round(retry_amplification_percent, 4),
            "accepted_request_p95_ms": _number(accepted_latency, "p95_ms") or 0.0,
            "accepted_request_p99_ms": _number(accepted_latency, "p99_ms") or 0.0,
            "logical_p95_ms": logical_p95,
            "logical_p99_ms": logical_p99,
            "logical_final_failure_rate_percent": final_failure_rate,
            "outcome_evidence": {
                "logical": logical_outcome,
                "raw_http": raw_outcome,
            },
            "note": "Stress acceptance does not apply the normal-traffic final-failure SLO.",
            "spike_metrics": spike_metrics,
            "timing_evidence": timing_evidence,
            "phase_plan_evidence": phase_plan_evidence,
            "phase_population_evidence": phase_population_checks,
            "raw_logical_population_evidence": raw_logical_population_checks,
            "top_population_timing_evidence": top_population_timing_checks,
            "phase_budget_evidence": phase_budget_results,
            "phase_retry_evidence": phase_retry_evidence,
        }

    if kind == "capacity":
        phase_results: dict[str, Any] = {}
        qualifying_rates: list[float] = []
        stable_goodputs: list[float] = []
        slo_contract = acceptance_contract.get("slo")
        if not isinstance(slo_contract, dict):
            slo_contract = acceptance_contract
        elif "expected_statuses" in acceptance_contract:
            slo_contract = {
                **slo_contract,
                "expected_statuses": acceptance_contract["expected_statuses"],
            }
        for phase_name, phase in (phase_summaries or {}).items():
            if str(phase_name) in {"primary", "duplicate", "state", "capacity_ramp"}:
                # Duplicate evidence is validated by phase_plan_completeness;
                # it is not one of the capacity-rate target phases.  The
                # primary aggregate and state-read evidence have their own
                # population checks.
                continue
            if not isinstance(phase, dict):
                continue
            phase_logical = (
                phase.get("logical")
                if canonical_evidence
                else phase.get("logical") or phase.get("raw_http")
            ) or {}
            phase_raw = phase.get("raw_http") or {}
            phase_result = evaluate_acceptance(
                contract_ok=True,
                logical_summary=phase_logical,
                raw_http_summary=phase_raw,
                acceptance_contract=slo_contract,
                origin_observability=origin_observability,
                canonical_evidence=canonical_evidence,
                expected_logical_scope=inferred_logical_scope,
                expected_fixture_marker=expected_fixture_marker,
                expected_external_run_id=expected_external_run_id,
                require_exact_observer_binding=require_exact_observer_binding,
            )
            rate = _number(phase_logical, "target_logical_actions_per_second") or 0.0
            goodput = _number(
                phase_logical,
                "successful_goodput_actions_per_second",
            )
            phase_result["target_logical_actions_per_second"] = rate
            phase_result["successful_goodput_actions_per_second"] = goodput
            minimum_goodput = _number(
                acceptance_contract,
                "minimum_useful_goodput_actions_per_second",
            )
            if minimum_goodput is not None:
                phase_result.setdefault("checks", {})["successful_goodput_measured"] = (
                    goodput is not None
                )
                phase_result.setdefault("checks", {})["minimum_useful_goodput"] = (
                    goodput is not None and goodput >= minimum_goodput
                )
            phase_outcome = _outcome_consistency(phase_logical)
            phase_failure_budget = _number(
                slo_contract,
                "logical_final_failure_percent",
            )
            if phase_failure_budget is not None:
                phase_result.setdefault("checks", {})["logical_final_failure"] = (
                    bool(phase_outcome.get("complete"))
                    and float(
                        phase_outcome.get("actual_failure_rate_percent") or 0.0
                    )
                    <= phase_failure_budget
                )
            phase_result.setdefault("checks", {})["timing_complete"] = bool(
                timing_completeness(
                    logical_summary=phase_logical,
                    raw_http_summary=phase_raw,
                    require_metrics=canonical_evidence,
                )["complete"]
            )
            phase_result["passed"] = all(
                bool(value) for value in (phase_result.get("checks") or {}).values()
            )
            phase_results[phase_name] = phase_result
            if phase_result.get("passed"):
                qualifying_rates.append(rate)
            if goodput is not None:
                stable_goodputs.append(goodput)
        phase_completion = bool(phase_results) and all(
            isinstance(phase, dict)
            and timing_completeness(
                logical_summary=phase.get("logical"),
                raw_http_summary=phase.get("raw_http"),
                require_metrics=canonical_evidence,
            )["complete"]
            for name, phase in (phase_summaries or {}).items()
            if str(name) not in {"primary", "duplicate", "state", "capacity_ramp"}
        )
        origin_safety = None
        resource_contract = acceptance_contract.get("resource_safety")
        if origin_observability is not None and isinstance(resource_contract, dict):
            origin_safety = _origin_safety_checks(
                resource_contract,
                origin_observability,
                require_complete_diagnostics=(
                    require_exact_observer_binding or canonical_evidence
                ),
            )
        pending_origin = observer_required and origin_observability is None
        binding = None
        if require_exact_observer_binding or canonical_evidence:
            binding = _observer_binding_check(
                origin_observability or {},
                expected_fixture_marker=expected_fixture_marker,
                expected_external_run_id=expected_external_run_id,
            )
        origin_ok = origin_safety is None or bool(origin_safety.get("passed"))
        binding_ok = binding is None or bool(binding.get("complete"))
        experiment_complete = (
            contract_ok is True
            and all(outcome_checks.values())
            and bool(timing_evidence["complete"])
            and bool(stage_evidence["complete"])
            and bool(budget_evidence["complete"])
            and all(phase_population_checks.values())
            and all(raw_logical_population_checks.values())
            and all(top_population_timing_checks.values())
            and all(population_checks.values())
            and phase_completion
            and (not require_phase_budgets or bool(phase_plan_evidence["complete"]))
            and (not require_phase_budgets or phase_budgets_ok)
            and not pending_origin
            and origin_ok
            and binding_ok
        )
        target_passed = experiment_complete and bool(phase_results) and all(
            bool(phase.get("passed")) for phase in phase_results.values()
        )
        return {
            "passed": target_passed,
            "experiment_complete": experiment_complete,
            "target_passed": target_passed,
            "decision": (
                "CAPACITY TARGET PASS"
                if target_passed
                else "CAPACITY EXPERIMENT COMPLETE TARGET FAIL"
                if experiment_complete
                else "CAPACITY PENDING ORIGIN EVIDENCE"
                if pending_origin
                else "CAPACITY EXPERIMENT INCOMPLETE"
            ),
            "contract_ok": contract_ok is True,
            "outcome_evidence": {
                "logical": logical_outcome,
                "raw_http": raw_outcome,
            },
            "population_checks": population_checks,
            "phase_completion": phase_completion,
            "pending_origin_evidence": pending_origin,
            "phase_slo": phase_results,
            "slo_capacity_logical_actions_per_second": max(qualifying_rates, default=0.0),
            "max_stable_goodput_actions_per_second": max(stable_goodputs, default=0.0),
            "origin_safety": origin_safety,
            "observer_binding": binding,
            "note": "Capacity experiment completion is separate from SLO capacity and max stable goodput.",
            "timing_evidence": timing_evidence,
            "capacity_ramp_evidence": stage_evidence,
            "acceptance_budget_evidence": budget_evidence,
            "phase_plan_evidence": phase_plan_evidence,
            "phase_population_evidence": phase_population_checks,
            "raw_logical_population_evidence": raw_logical_population_checks,
            "top_population_timing_evidence": top_population_timing_checks,
            "phase_budget_evidence": phase_budget_results,
            "phase_retry_evidence": phase_retry_evidence,
        }

    # SLO and spike profiles share the user-visible SLO contract.  Spike also
    # reports phase-level pressure and recovery; it must not be flattened into
    # a single normal-traffic result.
    accepted_budget = acceptance_contract.get("accepted_request_latency") or {}
    logical_budget = acceptance_contract.get("logical_latency") or {}
    failure_budget = _number(acceptance_contract, "logical_final_failure_percent")
    shed_budget = _number(acceptance_contract, "max_shed_percent")
    retry_budget = _number(acceptance_contract, "max_retry_amplification_percent")
    checks = {
        "contract": contract_ok is True,
        "logical_final_failure": failure_budget is not None and final_failure_rate <= failure_budget,
        "timing_complete": bool(timing_evidence["complete"]),
        "capacity_ramp_evidence": bool(stage_evidence["complete"]),
        "acceptance_budget_evidence": bool(budget_evidence["complete"]),
        **outcome_checks,
        **phase_population_checks,
        **raw_logical_population_checks,
        **top_population_timing_checks,
        **population_checks,
    }
    for percentile_name in ("p50", "p90", "p95", "p99"):
        _budget_check(
            checks,
            f"accepted_{percentile_name}",
            _number(accepted_latency, f"{percentile_name}_ms"),
            _number(accepted_budget, f"{percentile_name}_ms"),
        )
    for percentile_name in ("p95", "p99"):
        _budget_check(
            checks,
            f"logical_{percentile_name}",
            _number(latency, f"{percentile_name}_ms"),
            _number(logical_budget, f"{percentile_name}_ms"),
        )
    if shed_budget is not None:
        checks["normal_overload_shedding"] = shed_percent <= shed_budget
    if retry_budget is not None:
        checks["retry_amplification_percent"] = retry_amplification_percent <= retry_budget
    origin_safety = None
    resource_contract = acceptance_contract.get("resource_safety")
    if origin_observability is not None and isinstance(resource_contract, dict):
        origin_safety = _origin_safety_checks(
            resource_contract,
            origin_observability,
            require_complete_diagnostics=(
                require_exact_observer_binding or canonical_evidence
            ),
        )
        checks["origin_safety"] = bool(origin_safety.get("passed"))
    binding = None
    if require_exact_observer_binding or canonical_evidence:
        binding = _observer_binding_check(
            origin_observability or {},
            expected_fixture_marker=expected_fixture_marker,
            expected_external_run_id=expected_external_run_id,
        )
        checks["observer_binding"] = bool(binding["complete"])
    minimum_goodput = _number(
        acceptance_contract,
        "minimum_useful_goodput_actions_per_second",
    )
    if minimum_goodput is not None:
        checks["successful_goodput_measured"] = goodput is not None
        checks["minimum_useful_goodput"] = (
            goodput is not None and goodput >= minimum_goodput
        )
    if require_phase_budgets:
        checks["phase_plan"] = bool(phase_plan_evidence["complete"])
        checks["phase_budgets"] = phase_budgets_ok
    pending_origin = observer_required and origin_observability is None
    passed = all(checks.values()) and not pending_origin
    result = {
        "passed": passed,
        "decision": "SLO PASS" if passed else "SLO FAIL",
        "contract_ok": contract_ok is True,
        "checks": checks,
        "accepted_request_p50_ms": _number(accepted_latency, "p50_ms") or 0.0,
        "accepted_request_p90_ms": _number(accepted_latency, "p90_ms") or 0.0,
        "accepted_request_p95_ms": _number(accepted_latency, "p95_ms") or 0.0,
        "accepted_request_p99_ms": _number(accepted_latency, "p99_ms") or 0.0,
        "logical_p95_ms": logical_p95,
        "logical_p99_ms": logical_p99,
        "logical_final_failure_rate_percent": final_failure_rate,
        "outcome_evidence": {
            "logical": logical_outcome,
            "raw_http": raw_outcome,
        },
        "shed_percent": shed_percent,
        "retry_amplification_percent": round(retry_amplification_percent, 4),
        "pending_origin_evidence": pending_origin,
        "origin_safety": origin_safety,
        "observer_binding": binding,
        "minimum_useful_goodput_actions_per_second": minimum_goodput,
        "timing_evidence": timing_evidence,
        "capacity_ramp_evidence": stage_evidence,
        "acceptance_budget_evidence": budget_evidence,
        "phase_plan_evidence": phase_plan_evidence,
        "phase_population_evidence": phase_population_checks,
        "raw_logical_population_evidence": raw_logical_population_checks,
        "top_population_timing_evidence": top_population_timing_checks,
        "phase_budget_evidence": phase_budget_results,
        "phase_retry_evidence": phase_retry_evidence,
    }
    if origin_safety is not None:
        result["origin_evidence"] = origin_safety
    return result
