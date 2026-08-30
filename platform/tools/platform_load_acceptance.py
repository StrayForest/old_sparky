"""Acceptance evaluation for canonical external load profiles.

The client always reports the complete HTTP and logical populations.  This
module is the only place where a profile turns those measurements into an
acceptance result.  In particular, stress results never inherit the normal
traffic final-failure SLO.
"""

from __future__ import annotations

from typing import Any


def _number(payload: dict[str, Any] | None, key: str) -> float | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _budget_check(
    checks: dict[str, bool],
    name: str,
    actual: float | None,
    budget: float | None,
) -> None:
    if budget is None:
        return
    checks[name] = actual is not None and actual <= budget


def _origin_safety_checks(
    contract: dict[str, Any],
    origin_observability: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate bounded origin evidence without treating CPU as saturation alone."""

    checks: dict[str, bool] = {}
    system = origin_observability.get("system") or {}
    server = origin_observability.get("server_request_perf_logs") or {}
    pool = server.get("pool_checkout_wait_ms") or {}
    waits = system.get("postgres_waits") or {}
    postgres_connections = system.get("postgres_established_connections") or {}
    cpu_per_core = system.get("cpu_per_core") or {}
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
        "postgres_connections",
        _number(postgres_connections, "max"),
        _number(contract, "max_postgres_connections"),
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

    required = bool(origin_observability.get("stop_file_seen")) and not bool(
        origin_observability.get("timed_out")
    )
    checks["observer_completed"] = required
    return {
        "checks": checks,
        "passed": bool(checks) and all(checks.values()),
        "evidence_scope": "origin_observer_summary",
        "missing_diagnostics": missing_diagnostics,
    }


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
) -> dict[str, Any]:
    """Return a versioned SLO, capacity, spike or stress decision.

    The first three optional budget arguments remain for compatibility with
    the original runner and its focused unit tests.  Canonical profiles pass
    ``acceptance_contract`` instead.
    """

    latency = logical_summary.get(
        "end_to_end_latency", logical_summary.get("latency", {})
    )
    accepted_latency = logical_summary.get("accepted_request_latency", {})
    if not accepted_latency:
        accepted_latency = logical_summary.get("latency", {})
    logical_p95 = _number(latency, "p95_ms") or 0.0
    logical_p99 = _number(latency, "p99_ms") or 0.0
    final_failure_rate = _number(logical_summary, "final_failure_rate_percent") or 0.0
    raw_http_summary = raw_http_summary or {}
    shed_percent = _number(raw_http_summary, "temporary_overload_rate_percent") or 0.0
    actions = _number(logical_summary, "actions") or _number(raw_http_summary, "requests") or 0.0
    retries = _number(logical_summary, "total_retries") or 0.0
    retry_amplification_percent = retries * 100 / max(1.0, actions)

    if acceptance_contract is None:
        failure_budget = 0.5 if final_failure_budget_percent is None else final_failure_budget_percent
        checks = {
            "contract": bool(contract_ok),
            "logical_p95": logical_p95 <= (p95_budget_ms or 0.0),
            "logical_p99": logical_p99 <= (p99_budget_ms or 0.0),
            "logical_final_failure": final_failure_rate <= failure_budget,
        }
        return {
            "passed": all(checks.values()),
            "decision": "SLO PASS" if all(checks.values()) else "SLO FAIL",
            "contract_ok": bool(contract_ok),
            "checks": checks,
            "p95_budget_ms": p95_budget_ms,
            "p99_budget_ms": p99_budget_ms,
            "p95_ms": logical_p95,
            "p99_ms": logical_p99,
            "accepted_request_p95_ms": _number(accepted_latency, "p95_ms") or 0.0,
            "accepted_request_p99_ms": _number(accepted_latency, "p99_ms") or 0.0,
            "logical_final_failure_rate_percent": final_failure_rate,
            "logical_final_failure_budget_percent": failure_budget,
            "logical_final_failure_budget_ok": checks["logical_final_failure"],
        }

    kind = str(acceptance_contract.get("kind") or "slo")
    if kind in {"stress", "spike"}:
        checks = {
            "contract": bool(contract_ok),
            "unexpected_statuses": int(raw_http_summary.get("unexpected_statuses") or 0) == 0,
            "goodput_positive": (
                _number(logical_summary, "successful_goodput_actions_per_second")
                or _number(raw_http_summary, "successful_goodput_actions_per_second")
                or _number(raw_http_summary, "requests_per_second")
                or 0.0
            ) > 0.0,
        }
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
            if isinstance(burst, dict) and isinstance(recovered, dict):
                burst_logical = burst.get("logical") or {}
                recovered_logical = recovered.get("logical") or {}
                burst_raw = burst.get("raw_http") or {}
                recovered_raw = recovered.get("raw_http") or {}
                spike_metrics = {
                    "burst": {
                        "accepted_request_latency": burst_logical.get("accepted_request_latency") or burst_logical.get("latency"),
                        "logical_latency": burst_logical.get("end_to_end_latency") or burst_logical.get("latency"),
                        "shed_percent": burst_raw.get("temporary_overload_rate_percent", 0),
                        "goodput": burst_logical.get("successful_goodput_actions_per_second") or 0,
                    },
                    "recovery": {
                        "accepted_request_latency": recovered_logical.get("accepted_request_latency") or recovered_logical.get("latency"),
                        "logical_latency": recovered_logical.get("end_to_end_latency") or recovered_logical.get("latency"),
                        "shed_percent": recovered_raw.get("temporary_overload_rate_percent", 0),
                        "goodput": recovered_logical.get("successful_goodput_actions_per_second") or 0,
                    },
                }
                checks["recovery_goodput_positive"] = float(spike_metrics["recovery"]["goodput"] or 0) > 0
        if origin_observability is None:
            return {
                "passed": False,
                "decision": "SPIKE PENDING ORIGIN EVIDENCE" if kind == "spike" else "STRESS PENDING ORIGIN EVIDENCE",
                "pending_origin_evidence": True,
                "contract_ok": bool(contract_ok),
                "checks": checks,
                "shed_percent": shed_percent,
                "retry_amplification_percent": round(retry_amplification_percent, 4),
                "logical_final_failure_rate_percent": final_failure_rate,
                "spike_metrics": spike_metrics,
            }
        origin = _origin_safety_checks(acceptance_contract, origin_observability)
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
            "contract_ok": bool(contract_ok),
            "checks": checks,
            "origin_safety": origin,
            "shed_percent": shed_percent,
            "retry_amplification_percent": round(retry_amplification_percent, 4),
            "logical_final_failure_rate_percent": final_failure_rate,
            "note": "Stress acceptance does not apply the normal-traffic final-failure SLO.",
            "spike_metrics": spike_metrics,
        }

    if kind == "capacity":
        phase_results: dict[str, Any] = {}
        qualifying_rates: list[float] = []
        stable_goodputs: list[float] = []
        slo_contract = acceptance_contract.get("slo")
        if not isinstance(slo_contract, dict):
            slo_contract = acceptance_contract
        for phase_name, phase in (phase_summaries or {}).items():
            if not isinstance(phase, dict):
                continue
            phase_logical = phase.get("logical") or phase.get("raw_http") or {}
            phase_raw = phase.get("raw_http") or {}
            phase_result = evaluate_acceptance(
                contract_ok=True,
                logical_summary=phase_logical,
                raw_http_summary=phase_raw,
                acceptance_contract=slo_contract,
            )
            rate = _number(phase_logical, "target_logical_actions_per_second") or 0.0
            goodput = _number(phase_logical, "successful_goodput_actions_per_second") or 0.0
            phase_result["target_logical_actions_per_second"] = rate
            phase_result["successful_goodput_actions_per_second"] = goodput
            phase_results[phase_name] = phase_result
            if phase_result.get("passed"):
                qualifying_rates.append(rate)
            stable_goodputs.append(goodput)
        origin_safety = None
        resource_contract = acceptance_contract.get("resource_safety")
        if origin_observability is not None and isinstance(resource_contract, dict):
            origin_safety = _origin_safety_checks(resource_contract, origin_observability)
        pending_origin = bool(acceptance_contract.get("require_origin_evidence")) and origin_observability is None
        passed = bool(contract_ok) and bool(phase_results) and not pending_origin and (
            origin_safety is None or bool(origin_safety.get("passed"))
        )
        return {
            "passed": passed,
            "decision": "CAPACITY EXPERIMENT COMPLETE" if passed else (
                "CAPACITY PENDING ORIGIN EVIDENCE" if pending_origin else "CAPACITY EXPERIMENT INCOMPLETE"
            ),
            "contract_ok": bool(contract_ok),
            "pending_origin_evidence": pending_origin,
            "phase_slo": phase_results,
            "slo_capacity_logical_actions_per_second": max(qualifying_rates, default=0.0),
            "max_stable_goodput_actions_per_second": max(stable_goodputs, default=0.0),
            "origin_safety": origin_safety,
            "note": "Capacity experiment completion is separate from SLO capacity and max stable goodput.",
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
        "contract": bool(contract_ok),
        "logical_final_failure": failure_budget is not None and final_failure_rate <= failure_budget,
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
        origin_safety = _origin_safety_checks(resource_contract, origin_observability)
        checks["origin_safety"] = bool(origin_safety.get("passed"))
    pending_origin = bool(acceptance_contract.get("require_origin_evidence")) and origin_observability is None
    passed = all(checks.values()) and not pending_origin
    result = {
        "passed": passed,
        "decision": "SLO PASS" if passed else "SLO FAIL",
        "contract_ok": bool(contract_ok),
        "checks": checks,
        "accepted_request_p50_ms": _number(accepted_latency, "p50_ms") or 0.0,
        "accepted_request_p90_ms": _number(accepted_latency, "p90_ms") or 0.0,
        "accepted_request_p95_ms": _number(accepted_latency, "p95_ms") or 0.0,
        "accepted_request_p99_ms": _number(accepted_latency, "p99_ms") or 0.0,
        "logical_p95_ms": logical_p95,
        "logical_p99_ms": logical_p99,
        "logical_final_failure_rate_percent": final_failure_rate,
        "shed_percent": shed_percent,
        "retry_amplification_percent": round(retry_amplification_percent, 4),
        "pending_origin_evidence": pending_origin,
        "origin_safety": origin_safety,
    }
    if origin_safety is not None:
        result["origin_evidence"] = origin_safety
    return result
