"""Acceptance evaluation for canonical external load profiles."""

from __future__ import annotations

from typing import Any


def evaluate_acceptance(
    *,
    contract_ok: bool,
    logical_summary: dict[str, Any],
    p95_budget_ms: float,
    p99_budget_ms: float,
    final_failure_budget_percent: float,
) -> dict[str, Any]:
    """Evaluate correctness and user-visible latency for one logical phase.

    The caller supplies every budget from the versioned profile.  This module
    owns the common acceptance calculation, so the HTTP client cannot silently
    introduce a second set of thresholds.
    """

    latency = logical_summary.get(
        "end_to_end_latency", logical_summary.get("latency", {})
    )
    p95_ms = float(latency.get("p95_ms") or 0)
    p99_ms = float(latency.get("p99_ms") or 0)
    accepted_latency = logical_summary.get("accepted_request_latency", {})
    final_failure_rate = float(
        logical_summary.get("final_failure_rate_percent") or 0
    )
    failure_budget_ok = final_failure_rate <= final_failure_budget_percent
    return {
        "passed": bool(
            contract_ok
            and p95_ms <= p95_budget_ms
            and p99_ms <= p99_budget_ms
            and failure_budget_ok
        ),
        "contract_ok": contract_ok,
        "p95_budget_ms": p95_budget_ms,
        "p99_budget_ms": p99_budget_ms,
        "p95_ms": p95_ms,
        "p99_ms": p99_ms,
        "accepted_request_p95_ms": float(accepted_latency.get("p95_ms") or 0),
        "accepted_request_p99_ms": float(accepted_latency.get("p99_ms") or 0),
        "logical_final_failure_rate_percent": final_failure_rate,
        "logical_final_failure_budget_percent": final_failure_budget_percent,
        "logical_final_failure_budget_ok": failure_budget_ok,
    }
