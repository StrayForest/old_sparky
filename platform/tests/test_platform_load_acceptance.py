from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.platform_load import (
    _acceptance_failure_is_budget_only,
    _acceptance_budget_check_names,
    _authored_ramp_budget_checks,
    _closed_capacity_completed_budget_failure,
    _closed_capacity_pending_origin_acceptance,
    _capacity_phase_budget_check_names,
    _capacity_phase_plan_check_names,
    _CAPACITY_LOGICAL_OUTCOME_CHECKS,
    _CAPACITY_RAW_OUTCOME_CHECKS,
    _CAPACITY_PHASE_RETRY_CHECKS,
    _CAPACITY_EMPTY_PHASE_BUDGET_CHECKS,
    _CAPACITY_PHASE_SLO_REQUIRED_CHECKS,
    _is_closed_pending_origin_candidate,
    _is_complete_observer_bound_decision,
    _report_phase_envelope,
    get_profile,
    load_profiles,
    profile_contract,
)
from tools.platform_load_runtime import PID_NAMESPACE_ISOLATION, WORKER_REPORT_SCHEMA
from tools.platform_load_acceptance import (
    _capacity_ramp_evidence,
    derive_expected_phase_plan,
    evaluate_acceptance,
    observer_evidence_required,
    phase_plan_completeness,
)


def latency(p50: float, p90: float, p95: float, p99: float) -> dict[str, float]:
    return {"p50_ms": p50, "p90_ms": p90, "p95_ms": p95, "p99_ms": p99}


def timing(count: int) -> dict[str, object]:
    return {
        "timing_schema": 2,
        "user_observed_latency": {
            "p50_ms": 50,
            "p90_ms": 75,
            "p95_ms": 100,
            "p99_ms": 200,
        },
        "partial": False,
        "dropped_work": 0,
        "missing_schedule_context": 0,
        "missing_timing_context": 0,
        "invalid_timing_context": 0,
        "expected_count": count,
        "submitted_count": count,
        "completed_count": count,
        "scheduled_count": count,
        "started_count": count,
        "actual_request_start_count": count,
        "actual_start_count": count,
        "response_completion_count": count,
        "user_observed_count": count,
    }


def phase_record(
    actions: int,
    *,
    rate: float | None = None,
    duration: float | None = None,
    shed_percent: float = 0,
    goodput: float | None = None,
    final_failures: int = 0,
) -> dict[str, object]:
    """Build a complete canonical phase fixture for plan/budget tests."""

    completed = max(0, actions)
    successes = max(0, completed - final_failures)
    logical: dict[str, object] = {
        "actions": actions,
        "final_successes": successes,
        "final_failures": final_failures,
        "final_failure_rate_percent": round(
            final_failures * 100 / max(1, actions),
            4,
        ),
        "total_retries": 0,
        "successful_goodput_actions_per_second": (
            float(successes) if goodput is None else goodput
        ),
        "wall_seconds": 1,
        "accepted_request_latency": latency(1, 1, 1, 1),
        "timing": timing(actions),
    }
    phase: dict[str, object] = {
        "configured_actions": actions,
        "submitted_actions": actions,
        "missing_actions": 0,
        "complete": True,
        "logical": logical,
        "raw_http": {
            "requests": actions,
            "errors": final_failures,
            "successful_responses": successes,
            "final_failure_rate_percent": round(
                final_failures * 100 / max(1, actions),
                4,
            ),
            "temporary_overload_responses": 0,
            "temporary_overload_rate_percent": shed_percent,
            "successful_goodput_actions_per_second": float(successes),
            "wall_seconds": 1,
            "unexpected_statuses": 0,
            "configured_logical_actions": actions,
            "submitted_logical_actions": actions,
            "missing_logical_actions": 0,
            "timing": timing(actions),
        },
    }
    if rate is not None:
        phase["target_logical_actions_per_second"] = rate
        logical["target_logical_actions_per_second"] = rate
    if duration is not None:
        phase["duration_seconds"] = duration
    return phase


def empty_duplicate_phase() -> dict[str, object]:
    phase = phase_record(0, goodput=0)
    phase.update(
        {
            "candidate_actions": 0,
            "completed_actions": 0,
        }
    )
    return phase


def canonical_metric(count: int, value: float = 1.0) -> dict[str, object]:
    present = value if count else None
    return {
        "count": count,
        "avg_ms": present,
        "p50_ms": present,
        "p90_ms": present,
        "p95_ms": present,
        "p99_ms": present,
        "max_ms": present,
    }


def canonical_timing(count: int) -> dict[str, object]:
    timing_summary: dict[str, object] = {
        "timing_schema": 2,
        "partial": False,
        "dropped_work": 0,
        "missing_schedule_context": 0,
        "missing_timing_context": 0,
        "invalid_timing_context": 0,
        "expected_count": count,
        "submitted_count": count,
        "completed_count": count,
        "scheduled_count": count,
        "started_count": count,
        "actual_request_start_count": count,
        "actual_start_count": count,
        "response_completion_count": count,
        "user_observed_count": count,
    }
    for metric_name in (
        "service_latency",
        "user_observed_latency",
        "executor_queue_wait",
        "schedule_delay",
        "late_start",
    ):
        timing_summary[metric_name] = canonical_metric(count)
    return timing_summary


def canonical_logical(actions: int = 1) -> dict[str, object]:
    return {
        "scope": "logical_user_actions",
        "actions": actions,
        "final_successes": actions,
        "final_failures": 0,
        "final_failure_rate_percent": 0,
        "total_retries": 0,
        "retry_amplification_percent": 0,
        "final_status_counts": {"200": actions},
        "successful_goodput_actions_per_second": float(actions),
        "wall_seconds": 1,
        "accepted_request_latency": latency(1, 1, 1, 1),
        "timing": canonical_timing(actions),
    }


def canonical_raw(requests: int = 1) -> dict[str, object]:
    return {
        "scope": "full_population",
        "requests": requests,
        "errors": 0,
        "successful_responses": requests,
        "final_failure_rate_percent": 0,
        "temporary_overload_responses": 0,
        "temporary_overload_rate_percent": 0,
        "unexpected_statuses": 0,
        "retry_attempts": 0,
        "total_retries": 0,
        "retry_amplification_percent": 0,
        "successful_goodput_actions_per_second": float(requests),
        "wall_seconds": 1,
        "status_counts": {"200": requests},
        "timing": canonical_timing(requests),
    }


SLO = {
    "kind": "slo",
    "accepted_request_latency": latency(250, 400, 600, 1000),
    "logical_latency": {"p95_ms": 600, "p99_ms": 1000},
    "logical_final_failure_percent": 0.5,
    "max_shed_percent": 0,
    "max_retry_amplification_percent": 0,
}


class LoadAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _closed_cli_report(profile_id: str, *, budget_miss: bool) -> tuple[dict[str, object], dict[str, object]]:
        profile = get_profile(profile_id)
        contract = profile_contract(profile)
        planned = contract["planned_work"]
        total = int(planned["logical_actions"])
        primary = int(planned["primary_logical_actions"])
        state = int(planned["state_read_requests"])
        actual = total + state
        phases: dict[str, object] = {
            "primary": {
                "configured_actions": primary,
                "submitted_actions": primary,
                "missing_actions": 0,
                "complete": True,
                "logical": canonical_logical(primary),
                "raw_http": canonical_raw(primary),
            },
            "duplicate": {
                **empty_duplicate_phase(),
                "candidate_actions": primary,
                "completed_actions": 0,
            },
            "state": {"raw_http": canonical_raw(state)},
        }
        if profile["traffic"].get("phases"):
            ramp_phases: dict[str, object] = {}
            authored_phases = profile["traffic"]["phases"]
            for index, authored in enumerate(authored_phases):
                count = int(authored["logical_actions"])
                logical = canonical_logical(count)
                logical["target_logical_actions_per_second"] = authored[
                    "target_logical_actions_per_second"
                ]
                if budget_miss and index == len(authored_phases) - 1:
                    logical["accepted_request_latency"] = latency(1, 1, 5_000, 6_000)
                    logical["timing"]["user_observed_latency"] = latency(
                        1, 1, 5_000, 6_000
                    )
                ramp_phases[authored["name"]] = {
                    "configured_actions": count,
                    "submitted_actions": count,
                    "missing_actions": 0,
                    "complete": True,
                    "target_logical_actions_per_second": authored[
                        "target_logical_actions_per_second"
                    ],
                    "duration_seconds": authored["duration_seconds"],
                    "logical": logical,
                    "raw_http": canonical_raw(count),
                }
            phases["ramp"] = {"phases": ramp_phases}
        logical = canonical_logical(total)
        logical.update(
            {
                "primary_actions": primary,
                "duplicate_actions": 0,
                "configured_duplicate_actions": 0,
                "state_read_requests": state,
            }
        )
        if budget_miss and not profile["traffic"].get("phases"):
            logical["accepted_request_latency"] = latency(1, 1, 5_000, 6_000)
            logical["timing"]["user_observed_latency"] = latency(1, 1, 5_000, 6_000)
        raw = canonical_raw(total)
        raw.update(
            {"state_read_requests": state, "total_requests_including_state": actual}
        )
        overall = canonical_raw(actual)
        contract.update(
            {
                "offered_logical_actions": total,
                "primary_http_attempts": primary,
                "http_attempts": actual,
                "total_http_attempts": actual,
                "runtime_http_budget": {
                    "planned_worst_case": planned["http_attempts"],
                    "max_http_attempts": profile["portfolio"]["request_budget"][
                        "max_http_attempts"
                    ],
                    "actual_http_attempts": actual,
                    "within_budget": actual
                    <= profile["portfolio"]["request_budget"]["max_http_attempts"],
                },
            }
        )
        marker = "preprod202610070000abcd"
        run_id = "123"
        report: dict[str, object] = {
            "schema": 1,
            "measurement_schema": 2,
            "timing_schema": 1,
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "profile_digest": contract["profile_digest"],
            "mode": profile["mode"],
            "environment": profile["portfolio"]["environment"],
            "source_git_sha": "a" * 40,
            "external_run_id": run_id,
            "fixture_marker": marker,
            "scope": "full_population",
            "load_contract": contract,
            "authoritative": True,
            "dispatchable": True,
            "phases": phases,
            "overall": {**overall, "scope": "full_population"},
            "raw_http": raw,
            "logical": logical,
            "acceptance": {"contract_ok": True},
            "worker_report_schema": WORKER_REPORT_SCHEMA,
            "report_complete": True,
            "namespace_closed": True,
            "isolation": PID_NAMESPACE_ISOLATION,
            "partial_work": False,
            "inflight_unknown": False,
            "worker_exit_code": 1,
            "runtime_supervisor": {
                "reason": "none",
                "returncode": 1,
                "isolation": PID_NAMESPACE_ISOLATION,
                "namespace_closed": True,
                "descendants_reaped": True,
                "partial_work": False,
                "inflight_unknown": False,
                "report_error": None,
            },
        }
        return profile, report

    @staticmethod
    def _capacity_acceptance(*, pending: bool, budget_miss: bool) -> tuple[dict[str, object], dict[str, object]]:
        profile = get_profile("ready-vote-capacity-ramp-v2")
        phases = profile["traffic"]["phases"]
        phase_names = [str(phase["name"]) for phase in phases]
        phase_results: dict[str, object] = {}
        phase_budgets: dict[str, object] = {}
        for index, name in enumerate(phase_names):
            failed = budget_miss and index == len(phase_names) - 1
            phase_results[name] = {
                "passed": False if pending or failed else True,
                "pending_origin_evidence": pending,
                "contract_ok": True,
                "decision": "SLO FAIL" if pending or failed else "SLO PASS",
                "observer_binding": {
                    "complete": not pending,
                    "fixture_marker": None if pending else "preprod202610070000abcd",
                    "external_run_id": None if pending else "123",
                    "expected_fixture_marker": None,
                    "expected_external_run_id": None,
                    "checks": {
                        "fixture_marker": not pending,
                        "external_run_id": not pending,
                        "binding_complete": not pending,
                        "observer_stop_file_seen": not pending,
                        "observer_not_timed_out": not pending,
                    },
                },
                "checks": {
                    name: (
                        False
                        if (name == "accepted_p95" and failed)
                        or (name == "observer_binding" and pending)
                        else True
                    )
                    for name in _CAPACITY_PHASE_SLO_REQUIRED_CHECKS
                },
            }
            budget_checks = {
                name: not (failed and name == "accepted_p95")
                for name in _capacity_phase_budget_check_names(profile)
            }
            phase_budgets[name] = {
                "passed": all(budget_checks.values()),
                "checks": budget_checks,
            }
        primary_checks = {
            name: not (budget_miss and name == "accepted_p95")
            for name in _capacity_phase_budget_check_names(profile)
        }
        phase_budgets["primary"] = {
            "passed": all(primary_checks.values()),
            "checks": primary_checks,
        }
        phase_budgets["duplicate"] = {
            "passed": True,
            "checks": {
                name: True for name in _CAPACITY_EMPTY_PHASE_BUDGET_CHECKS
            },
        }
        acceptance: dict[str, object] = {
            "decision": "CAPACITY PENDING ORIGIN EVIDENCE" if pending else "CAPACITY EXPERIMENT COMPLETE TARGET FAIL",
            "experiment_complete": not pending,
            "target_passed": False,
            "passed": False,
            "contract_ok": True,
            "phase_completion": True,
            "pending_origin_evidence": pending,
            "max_stable_goodput_actions_per_second": 0.0,
            "slo_capacity_logical_actions_per_second": 0.0,
            "note": "Capacity experiment completion is separate from target budgets.",
            "origin_safety": None,
            "observer_binding": {
                "complete": False if pending else True,
                "fixture_marker": None if pending else "preprod202610070000abcd",
                "external_run_id": None if pending else "123",
                "expected_fixture_marker": None,
                "expected_external_run_id": None,
                "checks": {
                    "fixture_marker": not pending,
                    "external_run_id": not pending,
                    "binding_complete": not pending,
                    "observer_stop_file_seen": not pending,
                    "observer_not_timed_out": not pending,
                },
            },
            "population_checks": {"all_phases": True},
            "phase_population_evidence": {"all_phases": True},
            "raw_logical_population_evidence": {"all_populations": True},
            "top_population_timing_evidence": {"all_populations": True},
            "timing_evidence": {"complete": True},
            "phase_plan_evidence": {
                "complete": True,
                "checks": {
                    name: True for name in _capacity_phase_plan_check_names(profile)
                },
                "phase_budgets": phase_budgets,
            },
            "outcome_evidence": {
                "logical": {
                    "present": True,
                    "complete": True,
                    "kind": "logical",
                    "actions": 10,
                    "final_successes": 10,
                    "final_failures": 0,
                    "expected_failure_rate_percent": 0.0,
                    "actual_failure_rate_percent": 0.0,
                    "checks": {
                        name: True for name in _CAPACITY_LOGICAL_OUTCOME_CHECKS
                    },
                },
                "raw_http": {
                    "present": True,
                    "complete": True,
                    "kind": "raw_http",
                    "requests": 10,
                    "errors": 0,
                    "successful_responses": 10,
                    "expected_failure_rate_percent": 0.0,
                    "actual_failure_rate_percent": 0.0,
                    "checks": {
                        name: True for name in _CAPACITY_RAW_OUTCOME_CHECKS
                    },
                },
            },
            "phase_slo": phase_results,
            "phase_budget_evidence": phase_budgets,
            "capacity_ramp_evidence": {"complete": True, "checks": {}, "stages": {}},
            "phase_retry_evidence": {
                name: True for name in _CAPACITY_PHASE_RETRY_CHECKS
            },
        }
        acceptance["acceptance_budget_evidence"] = {
            "complete": True,
            "checks": {
                name: True for name in _acceptance_budget_check_names(profile)
            },
        }
        return profile, acceptance

    @staticmethod
    def _closed_pending_candidate() -> tuple[dict[str, object], SimpleNamespace]:
        profile = get_profile("ready-vote-slo-v2")
        contract = profile_contract(profile)
        planned = contract["planned_work"]
        logical_actions = int(planned["logical_actions"])
        primary_actions = int(planned["primary_logical_actions"])
        state_reads = int(planned["state_read_requests"])
        actual_requests = logical_actions + state_reads
        max_http_attempts = int(profile["portfolio"]["request_budget"]["max_http_attempts"])
        contract.update(
            {
                "offered_logical_actions": logical_actions,
                "primary_http_attempts": primary_actions,
                "http_attempts": actual_requests,
                "total_http_attempts": actual_requests,
                "runtime_http_budget": {
                    "planned_worst_case": planned["http_attempts"],
                    "max_http_attempts": max_http_attempts,
                    "actual_http_attempts": actual_requests,
                    "within_budget": actual_requests <= max_http_attempts,
                },
            }
        )
        report: dict[str, object] = {
            "schema": 1,
            "measurement_schema": 2,
            "timing_schema": 1,
            "profile_id": profile["profile_id"],
            "profile_version": profile["profile_version"],
            "profile_digest": contract["profile_digest"],
            "mode": profile["mode"],
            "environment": profile["portfolio"]["environment"],
            "source_git_sha": "a" * 40,
            "external_run_id": "123",
            "fixture_marker": "preprod202610070000abcd",
            "scope": "full_population",
            "load_contract": contract,
            "authoritative": True,
            "dispatchable": True,
            "phases": {"primary": {}, "duplicate": {}, "state": {}},
            "overall": {
                "scope": "full_population",
                "requests": actual_requests,
                "errors": 0,
                "successful_responses": actual_requests,
            },
            "raw_http": {
                "scope": "full_population",
                "requests": logical_actions,
                "state_read_requests": state_reads,
                "total_requests_including_state": actual_requests,
            },
            "logical": {
                "scope": "logical_user_actions",
                "actions": logical_actions,
            },
            "acceptance": {
                "passed": False,
                "decision": "SLO FAIL",
                "pending_origin_evidence": True,
                "contract_ok": True,
                "checks": {"timing_complete": True},
                "observer_binding": {
                    "complete": False,
                    "fixture_marker": None,
                    "external_run_id": None,
                    "expected_fixture_marker": None,
                    "expected_external_run_id": None,
                    "checks": {
                        "fixture_marker": False,
                        "external_run_id": False,
                        "binding_complete": False,
                        "observer_stop_file_seen": False,
                        "observer_not_timed_out": False,
                    },
                },
                "capacity_ramp_evidence": {
                    "complete": True,
                    "checks": {},
                    "stages": {},
                },
            },
            "worker_report_schema": WORKER_REPORT_SCHEMA,
            "report_complete": True,
            "namespace_closed": True,
            "isolation": PID_NAMESPACE_ISOLATION,
            "partial_work": False,
            "inflight_unknown": False,
            "worker_exit_code": 1,
            "runtime_supervisor": {
                "reason": "none",
                "returncode": 1,
                "isolation": PID_NAMESPACE_ISOLATION,
                "namespace_closed": True,
                "descendants_reaped": True,
                "partial_work": False,
                "inflight_unknown": False,
                "report_error": None,
            },
        }
        result = SimpleNamespace(
            returncode=1,
            report=report,
            worker_started=True,
            worker_exited=True,
            killed=False,
            signal=None,
            reason="none",
            partial_work=False,
            inflight_unknown=False,
            descendants_reaped=True,
            isolation=PID_NAMESPACE_ISOLATION,
            namespace_closed=True,
        )
        return profile, result

    @staticmethod
    def _complete_observer_bound_budget_miss() -> tuple[dict[str, object], dict[str, object]]:
        profile = get_profile("ready-vote-slo-v2")
        report: dict[str, object] = {
            "authoritative": True,
            "dispatchable": True,
            "partial_work": False,
            "inflight_unknown": False,
            "report_binding": {"complete": True},
            "runtime_supervisor": {
                "reason": "none",
                "namespace_closed": True,
                "descendants_reaped": True,
                "partial_work": False,
                "inflight_unknown": False,
                "report_error": None,
            },
            "origin_observability": {"binding": {"complete": True}},
            "acceptance": {
                "decision": "SLO FAIL",
                "passed": False,
                "pending_origin_evidence": False,
                "contract_ok": True,
                "observer_binding": {"complete": True},
                "origin_safety": {
                    "passed": True,
                    "checks": {
                        "observer_completed": True,
                        "required_diagnostics_present": True,
                        "pool_checkout_p95_ms": True,
                        "pool_checkout_p99_ms": True,
                        "postgres_backend_connections": True,
                        "waiting_backends": True,
                        "lock_waiters": True,
                        "cpu_per_core": True,
                    },
                },
                "timing_evidence": {"complete": True},
                "phase_plan_evidence": {"complete": True},
                "phase_population_evidence": {"all_phases": True},
                "raw_logical_population_evidence": {"all_populations": True},
                "top_population_timing_evidence": {"all_populations": True},
                "checks": {
                    "accepted_p95": False,
                    "timing_complete": True,
                    "origin_safety": True,
                },
            },
        }
        return profile, report

    def test_partial_timing_population_fails_closed(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 9,
                "final_failure_rate_percent": 0,
                "timing": {"partial": True},
                "end_to_end_latency": {"p95_ms": 100, "p99_ms": 200},
                "accepted_request_latency": latency(50, 75, 100, 200),
            },
            raw_http_summary={"temporary_overload_rate_percent": 0},
            acceptance_contract=SLO,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["timing_complete"])

    def test_slo_fast_rows_without_timing_evidence_cannot_green(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 100,
                "final_failure_rate_percent": 0,
                "end_to_end_latency": {"p95_ms": 10, "p99_ms": 20},
                "accepted_request_latency": latency(10, 15, 20, 30),
            },
            raw_http_summary={
                "requests": 100,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
            },
            acceptance_contract=SLO,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["timing_complete"])

    def test_exact_observer_binding_implies_origin_evidence(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 10,
                "final_failure_rate_percent": 0,
                "end_to_end_latency": {"p95_ms": 10, "p99_ms": 20},
                "accepted_request_latency": latency(10, 15, 20, 30),
                "timing": timing(10),
            },
            raw_http_summary={
                "requests": 10,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
                "timing": timing(10),
            },
            # This is the read-mix-human-v2 shape: exact binding is enabled
            # while the older contract omitted the redundant origin flag.
            acceptance_contract={**SLO},
            require_exact_observer_binding=True,
        )

        self.assertTrue(
            observer_evidence_required(
                SLO,
                require_exact_observer_binding=True,
            )
        )
        self.assertFalse(result["passed"])
        self.assertTrue(result["pending_origin_evidence"])
        self.assertFalse(result["checks"]["observer_binding"])
        self.assertFalse(result["observer_binding"]["complete"])
        self.assertEqual(result["decision"], "SLO FAIL")

    def test_stress_fast_rows_without_timing_evidence_cannot_green(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 100,
                "final_successes": 40,
                "final_failures": 60,
                "final_failure_rate_percent": 60,
                "successful_goodput_actions_per_second": 20,
                "accepted_request_latency": latency(100, 300, 500, 700),
            },
            raw_http_summary={
                "requests": 100,
                "temporary_overload_rate_percent": 60,
                "unexpected_statuses": 0,
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1500, 3000, 5000, 8000),
                "max_shed_percent": 99.9,
                "max_retry_amplification_percent": 100,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={"stop_file_seen": True, "timed_out": False},
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["timing_complete"])

    def test_spike_fast_phases_without_timing_evidence_cannot_green(self) -> None:
        phase = {
            "logical": {
                "actions": 10,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 10,
                "accepted_request_latency": latency(100, 200, 300, 400),
                "end_to_end_latency": latency(100, 200, 300, 400),
            },
            "raw_http": {
                "requests": 10,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
            },
        }
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 10,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 10,
                "accepted_request_latency": latency(100, 200, 300, 400),
                "end_to_end_latency": latency(100, 200, 300, 400),
                "timing": timing(10),
            },
            raw_http_summary={
                "requests": 10,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
                "timing": timing(10),
            },
            acceptance_contract={
                "kind": "spike",
                "accepted_request_latency": latency(1500, 3000, 5000, 8000),
                "max_shed_percent": 99.9,
                "max_retry_amplification_percent": 100,
                "recovery": {"burst_phase": "burst", "recovery_phase": "recovery"},
            },
            origin_observability={"stop_file_seen": True, "timed_out": False},
            phase_summaries={"burst": phase, "recovery": phase},
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["timing_complete"])
        self.assertFalse(result["checks"]["phase_completion"])

    def test_logical_acceptance_uses_user_observed_latency_when_available(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 10,
                "final_failure_rate_percent": 0,
                "end_to_end_latency": {"p95_ms": 10, "p99_ms": 20},
                "timing": {
                    "user_observed_latency": {"p95_ms": 2_000, "p99_ms": 2_500},
                },
                "accepted_request_latency": latency(10, 15, 20, 30),
            },
            raw_http_summary={"temporary_overload_rate_percent": 0},
            acceptance_contract={
                **SLO,
                "logical_latency": {"p95_ms": 100, "p99_ms": 200},
            },
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["logical_p95_ms"], 2_000)

    def test_pending_origin_evidence_is_not_green_standalone(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 10,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 10,
                "accepted_request_latency": latency(100, 200, 300, 400),
            },
            raw_http_summary={"temporary_overload_rate_percent": 0, "unexpected_statuses": 0},
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1500, 3000, 5000, 8000),
                "max_shed_percent": 99.9,
                "max_retry_amplification_percent": 100,
                "require_origin_evidence": True,
                "minimum_useful_goodput_actions_per_second": 1,
            },
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result["pending_origin_evidence"])

        profile, candidate = self._closed_pending_candidate()
        with patch.dict(
            os.environ,
            {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
            clear=False,
        ):
            self.assertTrue(_is_closed_pending_origin_candidate(profile, candidate))

            forged_budget_path = deepcopy(candidate)
            forged_budget_path.report["acceptance"]["unrelated"] = {
                "checks": {"accepted_p95": False}
            }
            self.assertFalse(
                _is_closed_pending_origin_candidate(profile, forged_budget_path)
            )

            forged_observer_path = deepcopy(candidate)
            forged_observer_path.report["acceptance"]["unrelated"] = {
                "checks": {"observer_binding": False}
            }
            self.assertFalse(
                _is_closed_pending_origin_candidate(profile, forged_observer_path)
            )

            forged_observer_leaf = deepcopy(candidate)
            forged_observer_leaf.report["acceptance"]["observer_binding"]["checks"][
                "unknown_observer_state"
            ] = False
            self.assertFalse(
                _is_closed_pending_origin_candidate(profile, forged_observer_leaf)
            )

            for status_key in ("complete", "passed", "target_passed"):
                with self.subTest(unexpected_status=status_key):
                    forged_status = deepcopy(candidate)
                    forged_status.report["acceptance"]["unexpected"] = {
                        status_key: False
                    }
                    self.assertFalse(
                        _is_closed_pending_origin_candidate(profile, forged_status)
                    )

            incomplete_phase = deepcopy(candidate)
            incomplete_phase.report["acceptance"]["phase_completion"] = False
            self.assertFalse(
                _is_closed_pending_origin_candidate(profile, incomplete_phase)
            )

            present_but_invalid_origin = deepcopy(candidate)
            present_but_invalid_origin.report["origin_observability"] = {
                "binding": {"complete": False}
            }
            self.assertFalse(
                _is_closed_pending_origin_candidate(profile, present_but_invalid_origin)
            )

            fabricated_origin_safety = deepcopy(candidate)
            fabricated_origin_safety.report["acceptance"]["origin_safety"] = {
                "passed": False,
                "complete": False,
                "checks": {"required_diagnostics_present": False},
            }
            self.assertFalse(
                _is_closed_pending_origin_candidate(profile, fabricated_origin_safety)
            )

        # A complete transport population with an authenticated-page-load
        # status-contract miss is not a deferred-origin candidate.  The raw
        # arithmetic reconciles, but the profile only accepts HTTP 200 and
        # the 316 HTTP 500 responses keep the report non-green.
        page_profile = get_profile("authenticated-page-load-v1")
        page_contract = profile_contract(page_profile)
        page_contract["offered_logical_actions"] = 20_000
        page_contract["primary_http_attempts"] = 20_000
        page_contract["http_attempts"] = 20_000
        page_contract["total_http_attempts"] = 20_000
        page_contract["runtime_http_budget"] = {
            "planned_worst_case": page_contract["planned_work"]["http_attempts"],
            "max_http_attempts": 20_000,
            "actual_http_attempts": 20_000,
            "within_budget": True,
        }
        mixed_status_summary: dict[str, object] = {
            "scope": "full_population",
            "requests": 20_000,
            "errors": 316,
            "successful_responses": 19_684,
            "final_failure_rate_percent": 1.58,
            "status_counts": {"200": 19_684, "500": 316},
            "unexpected_statuses": 316,
            "temporary_overload_responses": 0,
            "temporary_overload_rate_percent": 0.0,
            "retry_attempts": 0,
            "total_retries": 0,
            "wall_seconds": 575.291,
            "successful_goodput_actions_per_second": 19_684 / 575.291,
            "timing": {
                "timing_schema": 2,
                "expected_count": 20_000,
                "submitted_count": 20_000,
                "completed_count": 20_000,
                "scheduled_count": 20_000,
                "partial": False,
                "dropped_work": 0,
                "missing_schedule_context": 0,
                "missing_timing_context": 0,
                "invalid_timing_context": 0,
                "user_observed_latency": {
                    "p50_ms": 1_000,
                    "p90_ms": 2_000,
                    "p95_ms": 2_890.052,
                    "p99_ms": 3_576.125,
                },
            },
        }
        mixed_acceptance = evaluate_acceptance(
            contract_ok=False,
            logical_summary=mixed_status_summary,
            raw_http_summary=mixed_status_summary,
            acceptance_contract=page_contract["acceptance"],
            canonical_evidence=True,
            require_exact_observer_binding=True,
        )
        self.assertEqual(mixed_acceptance["decision"], "STRESS PENDING ORIGIN EVIDENCE")
        self.assertTrue(mixed_acceptance["pending_origin_evidence"])
        self.assertFalse(mixed_acceptance["contract_ok"])
        self.assertTrue(mixed_acceptance["outcome_evidence"]["raw_http"]["complete"])
        self.assertFalse(mixed_acceptance["checks"]["unexpected_statuses"])

        page_report = deepcopy(candidate.report)
        page_report.update(
            {
                "profile_id": page_profile["profile_id"],
                "profile_version": page_profile["profile_version"],
                "profile_digest": page_contract["profile_digest"],
                "mode": page_profile["mode"],
                "environment": page_profile["portfolio"]["environment"],
                "scope": "full_population",
                "load_contract": page_contract,
                "phases": {"authenticated_page_load": {}},
                "overall": mixed_status_summary,
                "raw_http": mixed_status_summary,
                "logical": mixed_status_summary,
                "acceptance": mixed_acceptance,
            }
        )
        page_candidate = SimpleNamespace(**vars(candidate))
        page_candidate.report = page_report
        with patch.dict(
            os.environ,
            {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
            clear=False,
        ):
            self.assertFalse(
                _is_closed_pending_origin_candidate(page_profile, page_candidate)
            )

        mutations = (
            ("worker return code", lambda result: setattr(result, "returncode", 2)),
            ("worker crashed", lambda result: setattr(result, "reason", "worker-exit")),
            ("worker killed", lambda result: setattr(result, "killed", True)),
            ("worker not exited", lambda result: setattr(result, "worker_exited", False)),
            ("namespace open", lambda result: setattr(result, "namespace_closed", False)),
            ("descendants not reaped", lambda result: setattr(result, "descendants_reaped", False)),
            ("partial work", lambda result: setattr(result, "partial_work", True)),
            ("inflight work unknown", lambda result: setattr(result, "inflight_unknown", True)),
            ("wrong worker isolation", lambda result: setattr(result, "isolation", "untrusted")),
            ("malformed worker report", lambda result: setattr(result, "report", [])),
            ("worker report incomplete", lambda result: result.report.update({"report_complete": False})),
            ("report namespace open", lambda result: result.report.update({"namespace_closed": False})),
            ("report SHA source mismatch", lambda result: result.report.update({"source_git_sha": "b" * 40})),
            ("report run mismatch", lambda result: result.report.update({"external_run_id": "124"})),
            ("worker and supervisor exit mismatch", lambda result: result.report.update({"worker_exit_code": 0})),
            ("unexpected pending decision", lambda result: result.report["acceptance"].update({"decision": "LOAD RUN FAILED"})),
            ("supervisor failed", lambda result: result.report["runtime_supervisor"].update({"reason": "worker-exit"})),
            ("supervisor report error", lambda result: result.report["runtime_supervisor"].update({"report_error": "invalid"})),
            ("supervisor descendants not reaped", lambda result: result.report["runtime_supervisor"].update({"descendants_reaped": False})),
            ("supervisor partial work", lambda result: result.report["runtime_supervisor"].update({"partial_work": True})),
            ("supervisor inflight unknown", lambda result: result.report["runtime_supervisor"].update({"inflight_unknown": True})),
        )
        with patch.dict(
            os.environ,
            {"SOURCE_GIT_SHA": "a" * 40, "GITHUB_RUN_ID": "123"},
            clear=False,
        ):
            for label, mutate in mutations:
                with self.subTest(failure=label):
                    invalid = deepcopy(candidate)
                    mutate(invalid)
                    self.assertFalse(_is_closed_pending_origin_candidate(profile, invalid))
        profile, report = self._complete_observer_bound_budget_miss()
        self.assertTrue(_is_complete_observer_bound_decision(profile, report))

        resource_budget_miss = deepcopy(report)
        resource_budget_miss["acceptance"]["origin_safety"] = {
            "passed": False,
            "checks": {
                "observer_completed": True,
                "required_diagnostics_present": True,
                "pool_checkout_p95_ms": True,
                "pool_checkout_p99_ms": True,
                "postgres_backend_connections": True,
                "waiting_backends": True,
                "lock_waiters": True,
                "cpu_per_core": False,
            },
        }
        resource_budget_miss["acceptance"]["checks"]["origin_safety"] = False
        self.assertTrue(
            _is_complete_observer_bound_decision(profile, resource_budget_miss)
        )

        ramp_profile = get_profile("read-mix-concurrency-ramp-v1")
        ramp_contract = profile_contract(ramp_profile)
        authored_stages = ramp_profile["traffic"]["concurrency_stages"]
        stage_counts = ramp_contract["planned_work"]["stage_logical_actions"]

        def ramp_evidence(
            *,
            population_mismatch: bool = False,
            stage_order: list[int] | None = None,
            sort_stage_keys: bool = False,
        ) -> dict[str, object]:
            stages = {
                str(stage): canonical_raw(stage_counts[str(stage)])
                for stage in authored_stages
            }
            for stage in stages.values():
                stage["latency"] = latency(1, 1, 1, 1)
            first_stage = stages[str(authored_stages[0])]
            first_stage["latency"] = latency(1, 1, 9000, 12000)
            if population_mismatch:
                first_stage["requests"] = int(first_stage["requests"]) - 1
            ramp = {
                "concurrency_stages": stage_order or authored_stages,
                "stages": stages,
            }
            if sort_stage_keys:
                ramp = json.loads(json.dumps(ramp, sort_keys=True))
            return _capacity_ramp_evidence(
                ramp,
                stage_counts,
                canonical=True,
                expected_statuses=frozenset(
                    ramp_profile["acceptance"]["expected_statuses"]
                ),
                acceptance_contract=ramp_profile["acceptance"],
                max_retries=0,
            )

        self.assertTrue(ramp_evidence(sort_stage_keys=True)["checks"]["ramp_stage_order"])
        self.assertFalse(
            ramp_evidence(stage_order=list(reversed(authored_stages)))["checks"][
                "ramp_stage_order"
            ]
        )
        self.assertFalse(
            ramp_evidence(stage_order=[True, *authored_stages[1:]])["checks"][
                "ramp_stage_order"
            ]
        )

        def ramp_decision(evidence: dict[str, object]) -> dict[str, object]:
            return {
                "authoritative": True,
                "dispatchable": True,
                "partial_work": False,
                "inflight_unknown": False,
                "report_binding": {"complete": True},
                "runtime_supervisor": {
                    "reason": "none",
                    "namespace_closed": True,
                    "descendants_reaped": True,
                    "partial_work": False,
                    "inflight_unknown": False,
                    "report_error": None,
                },
                "origin_observability": {"binding": {"complete": True}},
                "acceptance": {
                    "decision": "STRESS BEHAVIOR FAIL",
                    "passed": False,
                    "pending_origin_evidence": False,
                    "contract_ok": True,
                    "origin_safety": {
                        "passed": True,
                        "checks": {
                            "observer_completed": True,
                            "required_diagnostics_present": True,
                            "pool_checkout_p95_ms": True,
                            "pool_checkout_p99_ms": True,
                            "postgres_backend_connections": True,
                            "waiting_backends": True,
                            "lock_waiters": True,
                            "cpu_per_core": True,
                        },
                    },
                    "observer_binding": {"complete": True},
                    "timing_evidence": {"complete": True},
                    "phase_plan_evidence": {"complete": True},
                    "phase_population_evidence": {"all_phases": True},
                    "raw_logical_population_evidence": {"all_populations": True},
                    "top_population_timing_evidence": {"all_populations": True},
                    "capacity_ramp_evidence": evidence,
                    "checks": {
                        "accepted_p95_ms": True,
                        "timing_complete": True,
                        "origin_safety": True,
                        "capacity_ramp_evidence": evidence["complete"],
                    },
                },
            }

        complete_ramp_budget_miss = ramp_evidence()
        self.assertFalse(complete_ramp_budget_miss["complete"])
        self.assertTrue(
            all(
                value is True
                for name, value in complete_ramp_budget_miss["checks"].items()
                if name.endswith("_population") or name.startswith("ramp_")
                if not name.endswith("_budgets")
            )
        )
        self.assertTrue(
            _is_complete_observer_bound_decision(
                ramp_profile,
                ramp_decision(complete_ramp_budget_miss),
            )
        )

        incomplete_ramp_population = ramp_evidence(population_mismatch=True)
        self.assertFalse(incomplete_ramp_population["complete"])
        self.assertFalse(incomplete_ramp_population["checks"][f"ramp_{authored_stages[0]}_population"])
        self.assertFalse(
            _is_complete_observer_bound_decision(
                ramp_profile,
                ramp_decision(incomplete_ramp_population),
            )
        )

        structural_failures = (
            ("observer binding", "observer_binding", {"complete": False}),
            ("timing completeness", "timing_evidence", {"complete": False}),
        )
        for label, field, replacement in structural_failures:
            with self.subTest(failure=label):
                invalid = deepcopy(report)
                invalid["acceptance"][field] = replacement
                self.assertFalse(_is_complete_observer_bound_decision(profile, invalid))

        incomplete_origin_safety = deepcopy(report)
        incomplete_origin_safety["acceptance"].pop("origin_safety")
        self.assertFalse(
            _is_complete_observer_bound_decision(profile, incomplete_origin_safety)
        )

        hard_origin_failure = deepcopy(report)
        hard_origin_failure["acceptance"]["origin_safety"] = {
            "passed": False,
            "checks": {
                "observer_completed": False,
                "required_diagnostics_present": True,
                "pool_checkout_p95_ms": True,
                "pool_checkout_p99_ms": True,
                "postgres_backend_connections": True,
                "waiting_backends": True,
                "lock_waiters": True,
                "cpu_per_core": True,
            },
        }
        hard_origin_failure["acceptance"]["checks"]["origin_safety"] = False
        self.assertFalse(
            _is_complete_observer_bound_decision(profile, hard_origin_failure)
        )

        unknown_origin_check = deepcopy(report)
        unknown_origin_check["acceptance"]["origin_safety"]["checks"][
            "unrecognized_check"
        ] = True
        self.assertFalse(
            _is_complete_observer_bound_decision(profile, unknown_origin_check)
        )

        ownership_mismatch = deepcopy(report)
        ownership_mismatch["acceptance"]["origin_safety"]["checks"][
            "postgres_backend_ownership_consistent"
        ] = False
        ownership_mismatch["acceptance"]["origin_safety"]["passed"] = False
        ownership_mismatch["acceptance"]["checks"]["origin_safety"] = False
        self.assertFalse(
            _is_complete_observer_bound_decision(profile, ownership_mismatch)
        )

        invalid_check = deepcopy(report)
        invalid_check["acceptance"]["checks"]["timing_complete"] = False
        self.assertFalse(_is_complete_observer_bound_decision(profile, invalid_check))

    def test_top_level_origin_safety_profiles_classify_complete_budget_reds(self) -> None:
        _, base_report = self._complete_observer_bound_budget_miss()
        stress_profiles = [
            profile
            for profile in load_profiles().values()
            if profile["acceptance"].get("kind") in {"stress", "spike"}
        ]
        self.assertEqual(
            {profile["profile_id"] for profile in stress_profiles},
            {
                "authenticated-page-load-v1",
                "authenticated-page-load-v2",
                "read-mix-concurrency-ramp-v1",
                "read-mix-stress-v2",
                "ready-vote-saturation-ramp-v4",
                "ready-vote-spike-v1",
                "ready-vote-stress-15k-v2",
                "ready-vote-stress-20k-v2",
            },
        )
        resource_fields = (
            "max_postgres_backend_connections",
            "max_waiting_backends",
            "max_lock_waiters",
            "max_cpu_per_core_percent",
            "pool_checkout_wait_ms",
        )
        for profile in stress_profiles:
            with self.subTest(profile=profile["profile_id"]):
                self.assertTrue(
                    all(field in profile["acceptance"] for field in resource_fields)
                )
                report = deepcopy(base_report)
                kind = profile["acceptance"]["kind"]
                report["acceptance"]["decision"] = (
                    "SPIKE BEHAVIOR FAIL" if kind == "spike" else "STRESS BEHAVIOR FAIL"
                )
                report["acceptance"]["checks"].pop("accepted_p95")
                report["acceptance"]["checks"]["logical_p95_ms"] = False
                self.assertNotIn("resource_safety", profile["acceptance"])
                self.assertTrue(_is_complete_observer_bound_decision(profile, report))

        profile = get_profile("ready-vote-stress-15k-v2")
        report = deepcopy(base_report)
        report["acceptance"]["decision"] = "STRESS BEHAVIOR FAIL"
        report["acceptance"]["checks"].pop("accepted_p95")
        report["acceptance"]["checks"]["logical_p95_ms"] = False

        budget_safety_miss = deepcopy(report)
        budget_safety_miss["acceptance"]["origin_safety"]["checks"][
            "cpu_per_core"
        ] = False
        budget_safety_miss["acceptance"]["origin_safety"]["passed"] = False
        budget_safety_miss["acceptance"]["checks"]["origin_safety"] = False
        self.assertTrue(
            _is_complete_observer_bound_decision(profile, budget_safety_miss)
        )

        invalid_safety_mutations = (
            ("missing origin safety", lambda acceptance: acceptance.pop("origin_safety")),
            (
                "missing diagnostics",
                lambda acceptance: acceptance["origin_safety"]["checks"].update(
                    {"required_diagnostics_present": False}
                ),
            ),
            (
                "unknown safety check",
                lambda acceptance: acceptance["origin_safety"]["checks"].update(
                    {"unrecognized_check": True}
                ),
            ),
            (
                "non-boolean safety check",
                lambda acceptance: acceptance["origin_safety"]["checks"].update(
                    {"observer_completed": "true"}
                ),
            ),
        )
        for label, mutate in invalid_safety_mutations:
            with self.subTest(invalid_safety=label):
                invalid = deepcopy(report)
                mutate(invalid["acceptance"])
                self.assertFalse(_is_complete_observer_bound_decision(profile, invalid))

        for profile_id in ("ready-vote-slo-v2", "read-mix-human-v2"):
            with self.subTest(profile=profile_id, outcome="complete-slo-red"):
                slo_profile = get_profile(profile_id)
                slo_report = deepcopy(base_report)
                slo_report["acceptance"]["decision"] = "SLO FAIL"
                if not isinstance(
                    slo_profile["acceptance"].get("resource_safety"), Mapping
                ):
                    slo_report["acceptance"].pop("origin_safety")
                    slo_report["acceptance"]["checks"].pop("origin_safety")
                self.assertTrue(
                    _is_complete_observer_bound_decision(slo_profile, slo_report)
                )

        capacity_profile, capacity_acceptance = self._capacity_acceptance(
            pending=False, budget_miss=True
        )
        capacity_acceptance["origin_safety"] = {
            "passed": True,
            "checks": {
                "observer_completed": True,
                "required_diagnostics_present": True,
                "pool_checkout_p95_ms": True,
                "pool_checkout_p99_ms": True,
                "postgres_backend_connections": True,
                "waiting_backends": True,
                "lock_waiters": True,
                "cpu_per_core": True,
            },
        }
        capacity_report = {
            "authoritative": True,
            "dispatchable": True,
            "partial_work": False,
            "inflight_unknown": False,
            "report_binding": {"complete": True},
            "runtime_supervisor": {
                "reason": "none",
                "namespace_closed": True,
                "descendants_reaped": True,
                "partial_work": False,
                "inflight_unknown": False,
                "report_error": None,
            },
            "origin_observability": {"binding": {"complete": True}},
            "acceptance": capacity_acceptance,
        }
        self.assertTrue(
            _is_complete_observer_bound_decision(capacity_profile, capacity_report)
        )

        # A profile without any configured resource limits may omit the origin
        # safety object, but attaching one without a corresponding contract is
        # still rejected as an unexpected shape.
        slo_profile, slo_report = self._complete_observer_bound_budget_miss()
        slo_profile["acceptance"].pop("resource_safety")
        slo_report["acceptance"].pop("origin_safety")
        slo_report["acceptance"]["checks"].pop("origin_safety")
        self.assertTrue(_is_complete_observer_bound_decision(slo_profile, slo_report))
        unexpected_safety = deepcopy(slo_report)
        unexpected_safety["acceptance"]["origin_safety"] = report["acceptance"][
            "origin_safety"
        ]
        unexpected_safety["acceptance"]["checks"]["origin_safety"] = True
        self.assertFalse(
            _is_complete_observer_bound_decision(slo_profile, unexpected_safety)
        )

    def test_json_object_phase_maps_bind_authored_order_to_measured_sequence(self) -> None:
        spike = get_profile("ready-vote-spike-v1")
        authored = spike["traffic"]["phases"]
        starts = [
            "2026-10-08T12:00:00+00:00",
            "2026-10-08T12:00:30+00:00",
            "2026-10-08T12:00:45+00:00",
        ]
        finishes = [
            "2026-10-08T12:00:30+00:00",
            "2026-10-08T12:00:45+00:00",
            "2026-10-08T12:01:15+00:00",
        ]

        def spike_report() -> dict[str, object]:
            phase_map = {}
            for index, phase in enumerate(authored):
                phase_map[phase["name"]] = {
                    "started_at": starts[index],
                    "finished_at": finishes[index],
                    "configured_actions": phase["logical_actions"],
                    "submitted_actions": phase["logical_actions"],
                    "missing_actions": 0,
                    "complete": True,
                }
            return {
                "phases": {
                    "primary": {},
                    "duplicate": {},
                    "state": {},
                    "ramp": {"phases": phase_map},
                }
            }

        serialized = spike_report()
        round_tripped = json.loads(json.dumps(serialized, sort_keys=True))
        self.assertTrue(
            _report_phase_envelope(spike, round_tripped)["checks"]["phase_plan_closed"]
        )

        reordered = spike_report()
        reordered_map = reordered["phases"]["ramp"]["phases"]
        reordered_map["normal-before"]["started_at"] = starts[1]
        reordered_map["normal-before"]["finished_at"] = finishes[1]
        reordered_map["burst"]["started_at"] = starts[0]
        reordered_map["burst"]["finished_at"] = finishes[0]
        self.assertFalse(
            _report_phase_envelope(spike, reordered)["checks"]["phase_plan_closed"]
        )

        overlap = spike_report()
        overlap["phases"]["ramp"]["phases"]["burst"]["started_at"] = starts[0]
        self.assertFalse(
            _report_phase_envelope(spike, overlap)["checks"]["phase_plan_closed"]
        )

        missing = spike_report()
        del missing["phases"]["ramp"]["phases"]["burst"]
        self.assertFalse(
            _report_phase_envelope(spike, missing)["checks"]["phase_plan_closed"]
        )

        extra = spike_report()
        extra["phases"]["ramp"]["phases"]["unexpected"] = {}
        self.assertFalse(
            _report_phase_envelope(spike, extra)["checks"]["phase_plan_closed"]
        )

        invalid_timestamp = spike_report()
        invalid_timestamp["phases"]["ramp"]["phases"]["burst"]["started_at"] = 1
        self.assertFalse(
            _report_phase_envelope(spike, invalid_timestamp)["checks"]["phase_plan_closed"]
        )

        read_mix = get_profile("read-mix-concurrency-ramp-v1")
        concurrency = read_mix["traffic"]["concurrency_stages"]
        stage_report = {
            "phases": {
                "read_mix": {},
                "manual_refresh": {},
                "capacity_ramp": {
                    "concurrency_stages": concurrency,
                    "stages": {str(stage): {} for stage in concurrency},
                },
            }
        }
        sorted_stage_report = json.loads(json.dumps(stage_report, sort_keys=True))
        stage_envelope = _report_phase_envelope(read_mix, sorted_stage_report)
        self.assertTrue(stage_envelope["checks"]["phase_plan_closed"])
        self.assertTrue(stage_envelope["checks"]["phase_values_mapping"])

        reordered_stages = deepcopy(stage_report)
        reordered_stages["phases"]["capacity_ramp"]["concurrency_stages"] = list(
            reversed(concurrency)
        )
        self.assertFalse(
            _report_phase_envelope(read_mix, reordered_stages)["checks"]["phase_plan_closed"]
        )
        missing_stage = deepcopy(stage_report)
        del missing_stage["phases"]["capacity_ramp"]["stages"][str(concurrency[0])]
        self.assertFalse(
            _report_phase_envelope(read_mix, missing_stage)["checks"]["phase_values_mapping"]
        )
        invalid_stage = deepcopy(stage_report)
        invalid_stage["phases"]["capacity_ramp"]["stages"][str(concurrency[0])] = []
        self.assertFalse(
            _report_phase_envelope(read_mix, invalid_stage)["checks"]["phase_values_mapping"]
        )

    def test_evaluator_emits_classifiable_top_level_resource_budget_red(self) -> None:
        active_profiles = {
            profile["profile_id"]
            for profile in load_profiles().values()
            if profile["portfolio"].get("class") in {"default", "diagnostic"}
            and profile["portfolio"].get("status") == "active"
        }
        self.assertEqual(
            active_profiles,
            {
                "authenticated-page-load-v1",
                "authenticated-page-load-v2",
                "read-mix-concurrency-ramp-v1",
                "read-mix-human-v2",
                "read-mix-stress-v2",
                "ready-vote-capacity-ramp-v2",
                "ready-vote-saturation-ramp-v4",
                "ready-vote-slo-v2",
                "ready-vote-spike-v1",
                "ready-vote-stress-15k-v2",
                "ready-vote-stress-20k-v2",
            },
        )

        fixture_marker = "preprod202610070000abcd"
        external_run_id = "123"

        def canonical_phase(
            actions: int,
            *,
            rate: int | None = None,
            duration: int | None = None,
        ) -> dict[str, object]:
            logical = canonical_logical(actions)
            logical["total_retries"] = 0
            raw_http = canonical_raw(actions)
            raw_http.update(
                {
                    "retry_attempts": 0,
                    "total_retries": 0,
                    "retry_amplification_percent": 0,
                    "configured_logical_actions": actions,
                    "submitted_logical_actions": actions,
                    "missing_logical_actions": 0,
                }
            )
            phase: dict[str, object] = {
                "configured_actions": actions,
                "submitted_actions": actions,
                "missing_actions": 0,
                "complete": True,
                "logical": logical,
                "raw_http": raw_http,
            }
            if rate is not None:
                phase["target_logical_actions_per_second"] = rate
                phase["duration_seconds"] = duration
                logical["target_logical_actions_per_second"] = rate
            return phase

        def evaluate_profile(
            profile_id: str,
            *,
            cpu_percent: int,
            logical_latency_miss: bool = False,
            missing_pool_diagnostic: bool = False,
        ) -> tuple[dict[str, object], dict[str, object]]:
            profile = get_profile(profile_id)
            planned = profile_contract(profile)["planned_work"]
            total = int(planned["logical_actions"])
            primary = int(planned["primary_logical_actions"])
            duplicate = int(planned["duplicate_logical_actions"])
            phase_counts = planned["phase_logical_actions"]
            logical = canonical_logical(total)
            logical.update(
                {
                    "primary_actions": primary,
                    "duplicate_actions": duplicate,
                    "configured_duplicate_actions": duplicate,
                }
            )

            def exceed_logical_latency(summary: dict[str, object]) -> None:
                metric = {
                    "count": int(summary.get("actions", primary)),
                    "avg_ms": 13000,
                    "p50_ms": 9000,
                    "p90_ms": 11000,
                    "p95_ms": 13000,
                    "p99_ms": 15000,
                    "max_ms": 17000,
                }
                summary["accepted_request_latency"] = dict(metric)
                timing = summary.get("timing")
                if isinstance(timing, dict):
                    timing["user_observed_latency"] = dict(metric)

            if logical_latency_miss:
                exceed_logical_latency(logical)
            raw_http = canonical_raw(total)
            phases = {"primary": canonical_phase(primary)}
            duplicate_phase = canonical_phase(duplicate)
            duplicate_phase.update(
                {"candidate_actions": primary, "completed_actions": duplicate}
            )
            duplicate_phase["logical"]["changed_counts"] = {"False": duplicate}
            phases["duplicate"] = duplicate_phase
            for authored in profile["traffic"].get("phases", []):
                phases[authored["name"]] = canonical_phase(
                    int(authored["logical_actions"]),
                    rate=int(authored["target_logical_actions_per_second"]),
                    duration=int(authored["duration_seconds"]),
                )

            observer: dict[str, object] = {
                "stop_file_seen": True,
                "timed_out": False,
                "binding": {
                    "complete": True,
                    "fixture_marker": fixture_marker,
                    "external_run_id": external_run_id,
                },
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": cpu_percent}},
                    "postgres_backend_connections": {"max": 1},
                    "postgres_waits": {
                        "max_waiting_backends": 0,
                        "max_lock_waiters": 0,
                    },
                },
                "server_request_perf_logs": {
                    "pool_checkout_wait_ms": {"p95_ms": 1, "p99_ms": 1}
                },
            }
            if missing_pool_diagnostic:
                observer.pop("server_request_perf_logs")

            expected_plan = derive_expected_phase_plan(
                mode=profile["mode"],
                authored_phase_plan=profile["traffic"].get("phases"),
                expected_phase_action_counts=phase_counts,
            )
            acceptance = evaluate_acceptance(
                contract_ok=True,
                logical_summary=logical,
                raw_http_summary=raw_http,
                acceptance_contract=profile["acceptance"],
                origin_observability=observer,
                phase_summaries=phases,
                canonical_evidence=True,
                expected_logical_scope="logical_user_actions",
                allowed_phase_names=set(phases),
                expected_phase_action_counts=phase_counts,
                expected_fixture_marker=fixture_marker,
                expected_external_run_id=external_run_id,
                require_exact_observer_binding=True,
                expected_phase_plan=expected_plan,
                expected_duplicate_count=int(
                    profile["traffic"].get("duplicate_count") or 0
                ),
                expected_primary_action_count=primary,
                expected_total_logical_action_count=total,
                max_retries=int(profile["traffic"]["retry"]["max_retries"]),
            )
            _base_profile, complete_outer_report = (
                self._complete_observer_bound_budget_miss()
            )
            complete_outer_report["acceptance"] = acceptance
            complete_outer_report["origin_observability"] = {
                "binding": {"complete": True}
            }
            return profile, complete_outer_report

        with self.subTest(profile="ready-vote-stress-15k-v2", result="logical-budget-red"):
            profile, report = evaluate_profile(
                "ready-vote-stress-15k-v2", cpu_percent=0, logical_latency_miss=True
            )
            acceptance = report["acceptance"]
            self.assertEqual(acceptance["decision"], "STRESS BEHAVIOR FAIL")
            self.assertFalse(acceptance["passed"])
            self.assertTrue(acceptance["origin_safety"]["passed"])
            self.assertTrue(acceptance["checks"]["phase_budgets"])
            self.assertTrue(
                _acceptance_failure_is_budget_only(
                    acceptance, "stress", allow_no_budget_failure=True
                ),
            )
            self.assertTrue(_is_complete_observer_bound_decision(profile, report))

        with self.subTest(profile="ready-vote-spike-v1", result="resource-budget-red"):
            profile, report = evaluate_profile("ready-vote-spike-v1", cpu_percent=101)
            acceptance = report["acceptance"]
            self.assertEqual(acceptance["decision"], "SPIKE BEHAVIOR FAIL")
            self.assertFalse(acceptance["passed"])
            self.assertEqual(
                {name for name, passed in acceptance["checks"].items() if not passed},
                {"origin_safety"},
            )
            self.assertTrue(
                _acceptance_failure_is_budget_only(
                    acceptance, "spike", allow_no_budget_failure=True
                )
            )
            self.assertTrue(_is_complete_observer_bound_decision(profile, report))

        for profile_id in (
            "ready-vote-stress-15k-v2",
            "ready-vote-spike-v1",
        ):
            with self.subTest(profile=profile_id, result="safe-pass"):
                profile, report = evaluate_profile(profile_id, cpu_percent=0)
                self.assertTrue(report["acceptance"]["passed"])
                self.assertFalse(
                    _is_complete_observer_bound_decision(profile, report)
                )

        profile, report = evaluate_profile(
            "ready-vote-stress-15k-v2",
            cpu_percent=0,
            missing_pool_diagnostic=True,
        )
        self.assertFalse(report["acceptance"]["passed"])
        self.assertFalse(
            report["acceptance"]["origin_safety"]["checks"][
                "required_diagnostics_present"
            ]
        )
        self.assertFalse(_is_complete_observer_bound_decision(profile, report))

    def test_capacity_pending_and_completed_budget_miss_are_closed_world(self) -> None:
        profile, pending = self._capacity_acceptance(pending=True, budget_miss=False)
        self.assertTrue(_closed_capacity_pending_origin_acceptance(profile, pending))

        budget_miss_profile, completed = self._capacity_acceptance(
            pending=False, budget_miss=True
        )
        self.assertTrue(
            _closed_capacity_completed_budget_failure(
                budget_miss_profile,
                completed,
                origin_budget_failure=False,
            )
        )

        malformed_cases = []
        missing_phase = deepcopy(pending)
        missing_phase["phase_budget_evidence"].pop("rate-80")
        malformed_cases.append(missing_phase)
        missing_population = deepcopy(pending)
        missing_population["population_checks"] = {}
        malformed_cases.append(missing_population)
        nested_observer_failure = deepcopy(pending)
        nested_observer_failure["phase_slo"]["rate-20"]["nested"] = {
            "checks": {"observer_binding": False}
        }
        malformed_cases.append(nested_observer_failure)
        unrelated_budget_name = deepcopy(pending)
        unrelated_budget_name["unrelated_diagnostic"] = {
            "checks": {"accepted_p95": False}
        }
        malformed_cases.append(unrelated_budget_name)
        unknown_failure = deepcopy(pending)
        unknown_failure["phase_slo"]["rate-20"]["checks"]["unclassified"] = False
        malformed_cases.append(unknown_failure)
        for malformed_value in (None, [], "invalid", 1):
            malformed_outcome = deepcopy(pending)
            malformed_outcome["outcome_evidence"]["logical"] = malformed_value
            malformed_cases.append(malformed_outcome)
            malformed_raw_outcome = deepcopy(pending)
            malformed_raw_outcome["outcome_evidence"]["raw_http"] = malformed_value
            malformed_cases.append(malformed_raw_outcome)
        for invalid in malformed_cases:
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    _closed_capacity_pending_origin_acceptance(profile, invalid)
                )

        incomplete = deepcopy(completed)
        incomplete["phase_plan_evidence"]["checks"] = {}
        self.assertFalse(
            _closed_capacity_completed_budget_failure(
                budget_miss_profile,
                incomplete,
                origin_budget_failure=False,
            )
        )

    def test_pending_read_mix_ramp_accepts_only_authored_budget_leaves(self) -> None:
        profile = get_profile("read-mix-concurrency-ramp-v1")
        stages = profile["traffic"]["concurrency_stages"]
        failing_stage = stages[-1]
        checks = {
            "ramp_present": True,
            "ramp_authored_stage_plan": True,
            "ramp_stages_present": True,
            "ramp_stage_names_closed": True,
            "ramp_stage_order": True,
        }
        for stage in stages:
            checks[f"ramp_{stage}_expected_count_typed"] = True
            checks[f"ramp_{stage}_population"] = True
            checks[f"ramp_{stage}_budgets"] = stage != failing_stage
        evidence = {
            "complete": False,
            "checks": checks,
            "stages": {
                str(stage): (
                    {"budget_evidence": {"checks": {"accepted_p95": False}}}
                    if stage == failing_stage
                    else {}
                )
                for stage in stages
            },
        }
        acceptance = {
            "checks": {
                "capacity_ramp_evidence": False,
                "observer_binding": False,
                "timing_complete": True,
            },
            "observer_binding": {
                "complete": False,
                "fixture_marker": None,
                "external_run_id": None,
                "expected_fixture_marker": None,
                "expected_external_run_id": None,
                "checks": {
                    "fixture_marker": False,
                    "external_run_id": False,
                    "binding_complete": False,
                    "observer_stop_file_seen": False,
                    "observer_not_timed_out": False,
                },
            },
            "capacity_ramp_evidence": evidence,
        }
        allowed = _authored_ramp_budget_checks(profile, acceptance)
        self.assertEqual(allowed, frozenset(f"ramp_{stage}_budgets" for stage in stages))
        self.assertTrue(
            _acceptance_failure_is_budget_only(
                acceptance,
                "stress",
                allowed_ramp_budget_checks=allowed,
                allow_no_budget_failure=True,
                allow_pending_observer_binding=True,
                allowed_phase_names=frozenset(
                    profile_contract(profile)["planned_work"]["phase_logical_actions"]
                ),
            )
        )

        missing_structural = deepcopy(acceptance)
        missing_structural["capacity_ramp_evidence"]["checks"].pop("ramp_stage_order")
        self.assertIsNone(_authored_ramp_budget_checks(profile, missing_structural))
        nested_observer = deepcopy(acceptance)
        nested_observer["capacity_ramp_evidence"]["stages"][str(failing_stage)][
            "budget_evidence"
        ]["checks"]["observer_binding"] = False
        self.assertFalse(
            _acceptance_failure_is_budget_only(
                nested_observer,
                "stress",
                allowed_ramp_budget_checks=allowed,
                allow_no_budget_failure=True,
                allow_pending_observer_binding=True,
            )
        )

    def test_exact_observer_binding_mismatch_fails_closed(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 10,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 10,
                "accepted_request_latency": latency(100, 200, 300, 400),
            },
            raw_http_summary={"temporary_overload_rate_percent": 0, "unexpected_statuses": 0},
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1500, 3000, 5000, 8000),
                "max_shed_percent": 99.9,
                "max_retry_amplification_percent": 100,
                "pool_checkout_wait_ms": {"p95_ms": 5000, "p99_ms": 10000},
                "max_postgres_backend_connections": 52,
                "max_waiting_backends": 20,
                "max_lock_waiters": 20,
                "max_cpu_per_core_percent": 100,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={
                "stop_file_seen": True,
                "timed_out": False,
                "binding": {
                    "fixture_marker": "preprod26082900000000ab",
                    "external_run_id": "wrong-run",
                    "complete": True,
                },
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": 50}},
                    "postgres_backend_connections": {"max": 30},
                    "postgres_waits": {"max_waiting_backends": 0, "max_lock_waiters": 0},
                },
                "server_request_perf_logs": {
                    "pool_checkout_wait_ms": {"p95_ms": 10, "p99_ms": 20},
                },
            },
            expected_fixture_marker="preprod26082900000000ab",
            expected_external_run_id="123",
            require_exact_observer_binding=True,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["observer_binding"])

    def test_minimum_goodput_does_not_fall_back_to_attempt_rate(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 10,
                "final_successes": 0,
                "final_failure_rate_percent": 100,
                "successful_goodput_actions_per_second": 0,
                "accepted_request_latency": latency(100, 200, 300, 400),
            },
            raw_http_summary={
                "requests": 10,
                "requests_per_second": 100,
                "temporary_overload_rate_percent": 100,
                "unexpected_statuses": 0,
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1500, 3000, 5000, 8000),
                "max_shed_percent": 100,
                "max_retry_amplification_percent": 100,
                "pool_checkout_wait_ms": {"p95_ms": 5000, "p99_ms": 10000},
                "max_postgres_backend_connections": 52,
                "max_waiting_backends": 20,
                "max_lock_waiters": 20,
                "max_cpu_per_core_percent": 100,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={
                "stop_file_seen": True,
                "timed_out": False,
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": 50}},
                    "postgres_backend_connections": {"max": 30},
                    "postgres_waits": {"max_waiting_backends": 0, "max_lock_waiters": 0},
                },
                "server_request_perf_logs": {
                    "pool_checkout_wait_ms": {"p95_ms": 10, "p99_ms": 20},
                },
            },
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["minimum_useful_goodput"])

    def test_stress_rejects_service_latency_when_user_observed_latency_exceeds_budget(self) -> None:
        observed = timing(1)
        observed["user_observed_latency"] = latency(90_000, 90_000, 90_000, 90_000)
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 1,
                "final_successes": 1,
                "final_failures": 0,
                "successful_goodput_actions_per_second": 1,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "end_to_end_latency": latency(1, 1, 1, 1),
                "user_observed_latency": latency(1, 1, 1, 1),
                "timing": observed,
            },
            raw_http_summary={
                "requests": 1,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
                "timing": timing(1),
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1, 1, 1, 1),
                "logical_latency": {"p95_ms": 1_000, "p99_ms": 2_000},
                "max_shed_percent": 0,
                "max_retry_amplification_percent": 0,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={"stop_file_seen": True, "timed_out": False},
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["logical_p95_ms"], 90_000)
        self.assertFalse(result["checks"]["logical_p95_ms"])
        self.assertFalse(result["checks"]["logical_p99_ms"])

    def test_stress_requires_measured_successful_goodput_for_failed_logical_actions(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 100,
                "final_successes": 1,
                "final_failures": 99,
                "final_failure_rate_percent": 99,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "end_to_end_latency": latency(1, 1, 1, 1),
                "timing": timing(100),
            },
            raw_http_summary={
                "requests": 100,
                "requests_per_second": 10_000,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
                "timing": timing(100),
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1, 1, 1, 1),
                "logical_latency": {"p95_ms": 1_000, "p99_ms": 2_000},
                "max_shed_percent": 0,
                "max_retry_amplification_percent": 0,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={"stop_file_seen": True, "timed_out": False},
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["successful_goodput_measured"])
        self.assertFalse(result["checks"]["minimum_useful_goodput"])

    def test_selected_phase_plan_rejects_partial_capacity_and_missing_spike_baseline(self) -> None:
        capacity_plan = [
            {
                "name": f"rate-{rate}",
                "target_logical_actions_per_second": rate,
                "duration_seconds": 30,
                "logical_actions": rate * 30,
            }
            for rate in (20, 30, 40, 50, 60, 70, 80)
        ]
        capacity = evaluate_acceptance(
            contract_ok=True,
            logical_summary={},
            raw_http_summary={},
            acceptance_contract={
                "kind": "capacity",
                "slo": SLO,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            phase_summaries={
                "rate-20": phase_record(
                    600,
                    rate=20,
                    duration=30,
                    goodput=20,
                ),
                "duplicate": empty_duplicate_phase(),
            },
            expected_phase_plan=capacity_plan,
            expected_duplicate_count=0,
        )

        spike_plan = [
            {
                "name": "normal-before",
                "target_logical_actions_per_second": 10,
                "duration_seconds": 30,
                "logical_actions": 300,
            },
            {
                "name": "burst",
                "target_logical_actions_per_second": 80,
                "duration_seconds": 15,
                "logical_actions": 1_200,
            },
            {
                "name": "normal-after",
                "target_logical_actions_per_second": 10,
                "duration_seconds": 30,
                "logical_actions": 300,
            },
        ]
        spike = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 2,
                "final_successes": 2,
                "final_failures": 0,
                "successful_goodput_actions_per_second": 2,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(2),
            },
            raw_http_summary={
                "requests": 2,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
                "timing": timing(2),
            },
            acceptance_contract={
                "kind": "spike",
                "accepted_request_latency": latency(1, 1, 1, 1),
                "logical_latency": {"p95_ms": 1_000, "p99_ms": 2_000},
                "max_shed_percent": 0,
                "max_retry_amplification_percent": 0,
                "minimum_useful_goodput_actions_per_second": 1,
                "recovery": {
                    "burst_phase": "burst",
                    "recovery_phase": "normal-after",
                },
            },
            origin_observability={"stop_file_seen": True, "timed_out": False},
            phase_summaries={
                "burst": phase_record(1, rate=80, duration=15, goodput=1),
                "normal-after": phase_record(1, rate=10, duration=30, goodput=1),
                "duplicate": empty_duplicate_phase(),
            },
            expected_phase_plan=spike_plan,
            expected_duplicate_count=0,
        )

        self.assertFalse(capacity["passed"])
        self.assertFalse(capacity["phase_plan_evidence"]["checks"]["phase_names_and_order"])
        self.assertFalse(capacity["experiment_complete"])
        self.assertFalse(spike["passed"])
        self.assertFalse(spike["checks"]["phase_plan"])

    def test_duplicate_phase_shedding_cannot_hide_in_aggregate_budget(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 11,
                "final_successes": 10,
                "final_failures": 1,
                "successful_goodput_actions_per_second": 10,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(11),
            },
            raw_http_summary={
                "requests": 11,
                "temporary_overload_rate_percent": 9.09,
                "unexpected_statuses": 0,
                "timing": timing(11),
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1, 1, 1, 1),
                "logical_latency": {"p95_ms": 1_000, "p99_ms": 2_000},
                "max_shed_percent": 10,
                "max_retry_amplification_percent": 0,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={"stop_file_seen": True, "timed_out": False},
            phase_summaries={
                "duplicate": {
                    **phase_record(1, shed_percent=100, goodput=1),
                    "candidate_actions": 1,
                    "completed_actions": 1,
                }
            },
            expected_phase_plan=[],
            expected_duplicate_count=1,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result["checks"]["shed_percent"])
        self.assertFalse(
            result["phase_budget_evidence"]["duplicate"]["checks"]["shed_percent"]
        )

    def test_stress_binds_primary_and_duplicate_populations_to_profile_counts(self) -> None:
        duplicate = phase_record(1, goodput=1)
        duplicate.update(
            {
                "configured_actions": 1_000,
                "missing_actions": 999,
                "complete": False,
            }
        )
        duplicate["raw_http"]["configured_logical_actions"] = 1_000
        duplicate["raw_http"]["missing_logical_actions"] = 999
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "scope": "logical_user_actions",
                "actions": 2,
                "primary_actions": 1,
                "duplicate_actions": 1,
                "configured_duplicate_actions": 1_000,
                "final_successes": 2,
                "final_failures": 0,
                "successful_goodput_actions_per_second": 1,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(2),
            },
            raw_http_summary={
                "requests": 2,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
                "timing": timing(2),
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1, 1, 1, 1),
                "logical_latency": {"p95_ms": 1_000, "p99_ms": 2_000},
                "max_shed_percent": 99.9,
                "max_retry_amplification_percent": 200,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={"stop_file_seen": True, "timed_out": False},
            phase_summaries={"duplicate": duplicate},
            expected_phase_plan=[],
            expected_duplicate_count=1_000,
            expected_primary_action_count=20_000,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["primary_population"])
        self.assertFalse(result["checks"]["logical_population"])
        self.assertFalse(result["checks"]["duplicate_aggregate"])
        self.assertFalse(
            result["phase_plan_evidence"]["checks"]["duplicate_population"]
        )

    def test_slo_fails_when_users_are_shed_even_if_accepted_latency_is_fast(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 100,
                "final_failure_rate_percent": 1,
                "end_to_end_latency": {"p95_ms": 200, "p99_ms": 300},
                "accepted_request_latency": latency(100, 200, 300, 400),
            },
            raw_http_summary={
                "requests": 101,
                "temporary_overload_rate_percent": 1,
                "unexpected_statuses": 0,
            },
            acceptance_contract=SLO,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["normal_overload_shedding"])

    def test_stress_does_not_apply_normal_final_failure_slo(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 100,
                "final_successes": 40,
                "final_failures": 60,
                "final_failure_rate_percent": 60,
                "total_retries": 40,
                "successful_goodput_actions_per_second": 20,
                "accepted_request_latency": latency(100, 300, 500, 700),
                "timing": timing(100),
            },
            raw_http_summary={
                "requests": 100,
                "errors": 60,
                "successful_responses": 40,
                "final_failure_rate_percent": 60,
                "temporary_overload_rate_percent": 60,
                "unexpected_statuses": 0,
                "timing": timing(100),
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1500, 3000, 5000, 8000),
                "max_shed_percent": 99.9,
                "max_retry_amplification_percent": 100,
                "max_postgres_backend_connections": 52,
                "max_waiting_backends": 20,
                "max_lock_waiters": 20,
                "max_cpu_per_core_percent": 100,
                "pool_checkout_wait_ms": {"p95_ms": 5000, "p99_ms": 10000},
            },
            origin_observability={
                "stop_file_seen": True,
                "timed_out": False,
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": 80}},
                    "postgres_backend_connections": {"max": 51},
                    "postgres_tcp_established_connections": {"max": 54},
                    "postgres_waits": {"max_waiting_backends": 1, "max_lock_waiters": 0},
                },
                "server_request_perf_logs": {
                    "pool_checkout_wait_ms": {"p95_ms": 100, "p99_ms": 200},
                },
            },
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["decision"], "STRESS BEHAVIOR PASS")
        self.assertEqual(result["logical_final_failure_rate_percent"], 60)

    def test_missing_required_pool_sample_fails_closed(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 10,
                "final_failure_rate_percent": 0,
                "end_to_end_latency": {"p95_ms": 300, "p99_ms": 500},
                "accepted_request_latency": latency(100, 200, 300, 500),
            },
            raw_http_summary={
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
            },
            acceptance_contract={
                **SLO,
                "resource_safety": {
                    "pool_checkout_wait_ms": {"p95_ms": 5000, "p99_ms": 10000},
                    "max_postgres_backend_connections": 52,
                    "max_waiting_backends": 20,
                    "max_lock_waiters": 20,
                    "max_cpu_per_core_percent": 100,
                },
                "require_origin_evidence": True,
                "minimum_useful_goodput_actions_per_second": 1,
            },
            origin_observability={
                "stop_file_seen": True,
                "timed_out": False,
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": 50}},
                    "postgres_backend_connections": {"max": 30},
                    "postgres_waits": {
                        "max_waiting_backends": 0,
                        "max_lock_waiters": 0,
                    },
                },
                "server_request_perf_logs": {
                    "logged_requests": 0,
                },
                "binding": {
                    "fixture_marker": "preprod26082900000000ab",
                    "external_run_id": "123",
                    "complete": True,
                },
            },
            expected_fixture_marker="preprod26082900000000ab",
            expected_external_run_id="123",
            require_exact_observer_binding=True,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["origin_safety"])
        self.assertFalse(result["origin_safety"]["checks"]["required_diagnostics_present"])
        self.assertEqual(
            result["origin_safety"]["missing_diagnostics"],
            ["pool_checkout_p95_ms", "pool_checkout_p99_ms"],
        )

    def test_tcp_socket_peak_does_not_fail_backend_safety_budget(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 1,
                "final_successes": 1,
                "final_failures": 0,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 1,
                "end_to_end_latency": {"p95_ms": 100, "p99_ms": 200},
                "accepted_request_latency": latency(50, 75, 100, 200),
                "timing": timing(1),
            },
            raw_http_summary={
                "requests": 1,
                "errors": 0,
                "successful_responses": 1,
                "final_failure_rate_percent": 0,
                "temporary_overload_rate_percent": 0,
                "unexpected_statuses": 0,
                "timing": timing(1),
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(150, 300, 500, 800),
                "max_shed_percent": 1,
                "max_retry_amplification_percent": 10,
                "max_postgres_backend_connections": 52,
                "max_waiting_backends": 20,
                "max_lock_waiters": 20,
                "max_cpu_per_core_percent": 100,
                "pool_checkout_wait_ms": {"p95_ms": 5000, "p99_ms": 10000},
            },
            origin_observability={
                "stop_file_seen": True,
                "timed_out": False,
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": 50}},
                    "postgres_backend_connections": {"max": 51},
                    "postgres_tcp_established_connections": {"max": 54},
                    "postgres_waits": {"max_waiting_backends": 0, "max_lock_waiters": 0},
                },
                "server_request_perf_logs": {
                    "pool_checkout_wait_ms": {"p95_ms": 10, "p99_ms": 20},
                },
            },
        )

        self.assertTrue(result["passed"])
        self.assertTrue(result["origin_safety"]["checks"]["postgres_backend_connections"])
        self.assertEqual(
            result["origin_safety"]["postgres_tcp_established_connections"]["max"],
            54,
        )

    def test_backend_peak_fails_even_when_tcp_socket_count_is_lower(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "actions": 1,
                "final_failure_rate_percent": 0,
                "end_to_end_latency": {"p95_ms": 100, "p99_ms": 200},
                "accepted_request_latency": latency(50, 75, 100, 200),
            },
            raw_http_summary={"temporary_overload_rate_percent": 0, "unexpected_statuses": 0},
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(150, 300, 500, 800),
                "max_shed_percent": 1,
                "max_retry_amplification_percent": 10,
                "max_postgres_backend_connections": 52,
                "max_waiting_backends": 20,
                "max_lock_waiters": 20,
                "max_cpu_per_core_percent": 100,
                "pool_checkout_wait_ms": {"p95_ms": 5000, "p99_ms": 10000},
            },
            origin_observability={
                "stop_file_seen": True,
                "timed_out": False,
                "system": {
                    "cpu_per_core": {"cpu0": {"max_percent": 50}},
                    "postgres_backend_connections": {"max": 53},
                    "postgres_tcp_established_connections": {"max": 51},
                    "postgres_waits": {"max_waiting_backends": 0, "max_lock_waiters": 0},
                },
                "server_request_perf_logs": {
                    "pool_checkout_wait_ms": {"p95_ms": 10, "p99_ms": 20},
                },
            },
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["origin_safety"]["checks"]["postgres_backend_connections"])

    def test_capacity_reports_slo_capacity_separately_from_goodput(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={},
            raw_http_summary={},
            acceptance_contract={
                "kind": "capacity",
                "slo": SLO,
                "capacity": {
                    "target_logical_actions_per_second": [20, 40],
                    "steady_duration_seconds": 30,
                },
            },
            phase_summaries={
                "rate-20": {
                    "logical": {
                        "target_logical_actions_per_second": 20,
                        "actions": 20,
                        "final_successes": 20,
                        "final_failures": 0,
                        "final_failure_rate_percent": 0,
                        "end_to_end_latency": {"p95_ms": 100, "p99_ms": 200},
                        "accepted_request_latency": latency(100, 200, 300, 400),
                        "successful_goodput_actions_per_second": 20,
                        "timing": timing(20),
                    },
                    "raw_http": {
                        "requests": 20,
                        "errors": 0,
                        "successful_responses": 20,
                        "final_failure_rate_percent": 0,
                        "temporary_overload_rate_percent": 0,
                        "timing": timing(20),
                    },
                },
                "rate-40": {
                    "logical": {
                        "target_logical_actions_per_second": 40,
                        "actions": 40,
                        "final_successes": 32,
                        "final_failures": 8,
                        "final_failure_rate_percent": 20,
                        "end_to_end_latency": {"p95_ms": 700, "p99_ms": 1100},
                        "accepted_request_latency": latency(100, 200, 700, 1100),
                    "successful_goodput_actions_per_second": 32,
                        "timing": timing(40),
                    },
                    "raw_http": {
                        "requests": 40,
                        "errors": 8,
                        "successful_responses": 32,
                        "final_failure_rate_percent": 20,
                        "temporary_overload_rate_percent": 20,
                        "timing": timing(40),
                    },
                },
            },
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result["experiment_complete"])
        self.assertFalse(result["target_passed"])
        self.assertEqual(result["decision"], "CAPACITY EXPERIMENT COMPLETE TARGET FAIL")
        self.assertEqual(result["slo_capacity_logical_actions_per_second"], 20)
        self.assertEqual(result["max_stable_goodput_actions_per_second"], 32)

    def test_slo_recomputes_outcomes_and_rejects_string_contract_flag(self) -> None:
        result = evaluate_acceptance(
            contract_ok="false",
            logical_summary={
                "scope": "logical_user_actions",
                "actions": 10,
                "final_successes": 10,
                "final_failures": 0,
                "final_failure_rate_percent": 99,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(10),
            },
            raw_http_summary={
                "scope": "full_population",
                "requests": 10,
                "errors": 0,
                "successful_responses": 10,
                "final_failure_rate_percent": 0,
                "temporary_overload_rate_percent": 0,
                "timing": timing(10),
            },
            acceptance_contract=SLO,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["contract_ok"])
        self.assertFalse(result["checks"]["contract"])
        self.assertFalse(result["checks"]["logical_outcome_consistency"])
        self.assertEqual(
            result["outcome_evidence"]["logical"]["expected_failure_rate_percent"],
            0.0,
        )

    def test_read_and_page_populations_require_the_profile_primary_work(self) -> None:
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "scope": "full_population",
                "requests": 0,
                "errors": 0,
                "successful_responses": 0,
                "final_failure_rate_percent": 0,
                "primary_actions": 0,
                "successful_goodput_actions_per_second": 30_000,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(0),
            },
            raw_http_summary={
                "scope": "full_population",
                "requests": 0,
                "errors": 0,
                "successful_responses": 0,
                "final_failure_rate_percent": 0,
                "temporary_overload_rate_percent": 0,
                "timing": timing(0),
            },
            acceptance_contract=SLO,
            expected_primary_action_count=30_000,
            expected_total_logical_action_count=30_000,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["primary_population"])
        self.assertFalse(result["checks"]["logical_population"])

    def test_ready_vote_state_reads_are_required_and_authoritatively_bound(self) -> None:
        duplicate = phase_record(1)
        duplicate["logical"]["changed_counts"] = {"False": 1}
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "scope": "logical_user_actions",
                "actions": 2,
                "primary_actions": 1,
                "duplicate_actions": 1,
                "configured_duplicate_actions": 1,
                "final_successes": 2,
                "final_failures": 0,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 2,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(2),
            },
            raw_http_summary={
                "scope": "full_population",
                "requests": 2,
                "errors": 0,
                "successful_responses": 2,
                "final_failure_rate_percent": 0,
                "temporary_overload_rate_percent": 0,
                "timing": timing(2),
            },
            acceptance_contract=SLO,
            phase_summaries={
                "primary": phase_record(1),
                "duplicate": duplicate,
            },
            expected_phase_plan=[],
            expected_duplicate_count=1,
            expected_primary_action_count=1,
            expected_total_logical_action_count=2,
            expected_state_read_count=1,
            max_retries=0,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["phase_plan"])
        self.assertFalse(result["checks"]["state_read_population"])
        self.assertFalse(
            result["phase_plan_evidence"]["checks"]["state_read_population"]
        )

    def test_duplicate_state_changes_fail_even_with_exact_duplicate_counts(self) -> None:
        duplicate = phase_record(1_000)
        duplicate["logical"]["changed_counts"] = {"True": 1_000}
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "scope": "logical_user_actions",
                "actions": 2_000,
                "primary_actions": 1_000,
                "duplicate_actions": 1_000,
                "configured_duplicate_actions": 1_000,
                "final_successes": 2_000,
                "final_failures": 0,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 2_000,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(2_000),
            },
            raw_http_summary={
                "scope": "full_population",
                "requests": 2_000,
                "errors": 0,
                "successful_responses": 2_000,
                "final_failure_rate_percent": 0,
                "temporary_overload_rate_percent": 0,
                "timing": timing(2_000),
            },
            acceptance_contract=SLO,
            phase_summaries={
                "primary": phase_record(1_000),
                "duplicate": duplicate,
            },
            expected_phase_plan=[],
            expected_duplicate_count=1_000,
            expected_primary_action_count=1_000,
            expected_total_logical_action_count=2_000,
            max_retries=0,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(
            result["phase_plan_evidence"]["checks"]["duplicate_population"]
        )

    def test_raw_http_phase_population_must_reconcile_with_logical_timing(self) -> None:
        phase = phase_record(10, rate=10, duration=1)
        phase["raw_http"].update(
            {
                "requests": 9,
                "errors": 0,
                "successful_responses": 9,
                "final_failure_rate_percent": 0,
                "timing": timing(9),
            }
        )
        result = evaluate_acceptance(
            contract_ok=True,
            logical_summary={
                "scope": "logical_user_actions",
                "actions": 10,
                "final_successes": 10,
                "final_failures": 0,
                "final_failure_rate_percent": 0,
                "successful_goodput_actions_per_second": 10,
                "accepted_request_latency": latency(1, 1, 1, 1),
                "timing": timing(10),
            },
            raw_http_summary={
                "scope": "full_population",
                "requests": 9,
                "errors": 0,
                "successful_responses": 9,
                "final_failure_rate_percent": 0,
                "temporary_overload_rate_percent": 0,
                "timing": timing(9),
            },
            acceptance_contract=SLO,
            phase_summaries={
                "read": phase,
                "duplicate": empty_duplicate_phase(),
            },
            expected_phase_plan=[
                {
                    "name": "read",
                    "target_logical_actions_per_second": 10,
                    "duration_seconds": 1,
                    "logical_actions": 10,
                }
            ],
            expected_duplicate_count=0,
            max_retries=0,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(
            result["checks"]["raw_requests_reconcile_to_logical_population"]
        )
        self.assertFalse(result["phase_plan_evidence"]["checks"]["phase_read"])
        self.assertFalse(
            result["phase_budget_evidence"]["read"]["checks"][
                "raw_requests_cover_logical_actions"
            ]
        )

    def test_canonical_closed_world_matrix_rejects_remaining_false_green_shapes(self) -> None:
        contract = {
            "kind": "stress",
            "accepted_request_latency": latency(10, 20, 30, 40),
            "logical_latency": {"p95_ms": 30, "p99_ms": 40},
            "max_shed_percent": 100,
            "max_retry_amplification_percent": 100,
            "minimum_useful_goodput_actions_per_second": 1,
            "max_postgres_backend_connections": 52,
            "max_waiting_backends": 20,
            "max_lock_waiters": 20,
            "max_cpu_per_core_percent": 100,
            "pool_checkout_wait_ms": {"p95_ms": 5000, "p99_ms": 10000},
            "expected_statuses": [200],
            "unexpected_statuses": 0,
        }
        origin = {
            "stop_file_seen": True,
            "timed_out": False,
            "binding": {
                "fixture_marker": "preprod26082900000000ab",
                "external_run_id": "123",
                "complete": True,
            },
            "system": {
                "cpu_per_core": {"cpu0": {"max_percent": 1}},
                "postgres_backend_connections": {"max": 1},
                "postgres_waits": {"max_waiting_backends": 0, "max_lock_waiters": 0},
            },
            "server_request_perf_logs": {
                "pool_checkout_wait_ms": {"p95_ms": 1, "p99_ms": 1},
            },
        }
        baseline = {
            "contract_ok": True,
            "logical_summary": canonical_logical(),
            "raw_http_summary": canonical_raw(),
            "acceptance_contract": contract,
            "origin_observability": origin,
            "canonical_evidence": True,
            "expected_logical_scope": "logical_user_actions",
            "allowed_phase_names": set(),
            "expected_fixture_marker": "preprod26082900000000ab",
            "expected_external_run_id": "123",
            "require_exact_observer_binding": True,
            "max_retries": 0,
        }
        accepted = evaluate_acceptance(**baseline)
        self.assertTrue(accepted["passed"])

        def scope_substitution(case: dict[str, object]) -> None:
            case["raw_http_summary"]["scope"] = "logical_user_actions"  # type: ignore[index]

        def fabricated_goodput(case: dict[str, object]) -> None:
            logical = case["logical_summary"]
            raw_http = case["raw_http_summary"]
            logical.update(
                {
                    "final_successes": 0,
                    "final_failures": 1,
                    "final_failure_rate_percent": 100,
                    "successful_goodput_actions_per_second": 1,
                    "final_status_counts": {"503": 1},
                }
            )
            raw_http.update(
                {
                    "errors": 1,
                    "successful_responses": 0,
                    "final_failure_rate_percent": 100,
                    "temporary_overload_rate_percent": 100,
                    "status_counts": {"503": 1},
                }
            )

        def retry_mismatch(case: dict[str, object]) -> None:
            logical = case["logical_summary"]
            raw_http = case["raw_http_summary"]
            raw_http.update(
                {
                    "requests": 2,
                    "successful_responses": 2,
                    "status_counts": {"200": 2},
                    "retry_attempts": 1,
                    "total_retries": 1,
                    "retry_amplification_percent": 100,
                    "timing": canonical_timing(2),
                }
            )
            logical["timing"] = canonical_timing(1)

        def invalid_budget(case: dict[str, object]) -> None:
            case["acceptance_contract"]["max_shed_percent"] = -1  # type: ignore[index]

        def string_resource_budget(case: dict[str, object]) -> None:
            case["acceptance_contract"]["resource_safety"] = {  # type: ignore[index]
                "max_postgres_backend_connections": "52",
            }

        def metric_count_mismatch(case: dict[str, object]) -> None:
            case["logical_summary"]["timing"]["service_latency"]["count"] = 0  # type: ignore[index]

        def percentile_order_mismatch(case: dict[str, object]) -> None:
            case["logical_summary"]["timing"]["service_latency"]["p50_ms"] = 3  # type: ignore[index]
            case["logical_summary"]["timing"]["service_latency"]["p90_ms"] = 2  # type: ignore[index]

        def unknown_phase(case: dict[str, object]) -> None:
            case["phase_summaries"] = {"rogue": {}}
            case["allowed_phase_names"] = set()

        def observer_id_type(case: dict[str, object]) -> None:
            case["origin_observability"]["binding"]["external_run_id"] = 123  # type: ignore[index]

        mutations = {
            "raw_logical_scope_substitution": scope_substitution,
            "zero_success_positive_goodput": fabricated_goodput,
            "raw_retry_population_mismatch": retry_mismatch,
            "negative_acceptance_budget": invalid_budget,
            "string_resource_budget": string_resource_budget,
            "nested_timing_count_mismatch": metric_count_mismatch,
            "nested_percentile_order": percentile_order_mismatch,
            "unknown_phase": unknown_phase,
            "observer_run_id_not_string": observer_id_type,
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                candidate = deepcopy(baseline)
                mutate(candidate)
                result = evaluate_acceptance(**candidate)
                self.assertFalse(result["passed"])

        ramp_contract = {
            "kind": "stress",
            "accepted_request_latency": latency(10, 20, 30, 40),
            "logical_latency": {"p95_ms": 30, "p99_ms": 40},
            "max_shed_percent": 100,
            "max_retry_amplification_percent": 0,
            "minimum_useful_goodput_actions_per_second": 1,
            "expected_statuses": [200],
            "unexpected_statuses": 0,
        }
        def ramp_raw(requests: int) -> dict[str, object]:
            summary = canonical_raw(requests)
            summary["latency"] = latency(1, 1, 1, 1)
            return summary

        ramp_stage = ramp_raw(1)
        ramp_baseline = {
            "contract_ok": True,
            "logical_summary": {**ramp_raw(2), "primary_actions": 2},
            "raw_http_summary": ramp_raw(2),
            "acceptance_contract": ramp_contract,
            "origin_observability": origin,
            "canonical_evidence": True,
            "expected_logical_scope": "full_population",
            "allowed_phase_names": {"read_mix", "capacity_ramp"},
            "expected_phase_plan": [{"name": "read_mix", "logical_actions": 2}],
            "expected_phase_action_counts": {"read_mix": 2},
            "expected_stage_action_counts": {"16": 1, "32": 1},
            "expected_primary_action_count": 2,
            "expected_total_logical_action_count": 2,
            "expected_fixture_marker": "preprod26082900000000ab",
            "expected_external_run_id": "123",
            "require_exact_observer_binding": True,
            "max_retries": 0,
            "phase_summaries": {
                "read_mix": {
                    "logical": ramp_raw(2),
                    "raw_http": ramp_raw(2),
                },
                "capacity_ramp": {
                    "concurrency_stages": [16, 32],
                    "stages": {"16": ramp_stage, "32": deepcopy(ramp_stage)},
                },
            },
        }
        self.assertTrue(evaluate_acceptance(**ramp_baseline)["passed"])

        def missing_capacity_ramp(case: dict[str, object]) -> None:
            del case["phase_summaries"]["capacity_ramp"]  # type: ignore[index]

        def extra_capacity_auxiliary(case: dict[str, object]) -> None:
            case["phase_summaries"]["unexpected_auxiliary"] = {}  # type: ignore[index]

        def wrong_capacity_stage(case: dict[str, object]) -> None:
            case["phase_summaries"]["capacity_ramp"]["concurrency_stages"] = [  # type: ignore[index]
                16,
                64,
            ]

        ramp_mutations = {
            "missing_capacity_ramp": missing_capacity_ramp,
            "unknown_capacity_auxiliary": extra_capacity_auxiliary,
            "wrong_authored_capacity_stage": wrong_capacity_stage,
        }
        for name, mutate in ramp_mutations.items():
            with self.subTest(capacity_ramp_case=name):
                candidate = deepcopy(ramp_baseline)
                mutate(candidate)
                result = evaluate_acceptance(**candidate)
                self.assertFalse(result["passed"])
                if name == "wrong_authored_capacity_stage":
                    self.assertFalse(result["checks"]["capacity_ramp_evidence"])
                else:
                    self.assertFalse(result["checks"]["phase_plan"])
                    if name == "missing_capacity_ramp":
                        self.assertFalse(result["checks"]["capacity_ramp_evidence"])

    def test_duplicate_candidates_must_be_successful_primary_population(self) -> None:
        primary = phase_record(1)
        duplicate = phase_record(1)
        duplicate.update(
            {
                "candidate_actions": 0,
                "completed_actions": 1,
            }
        )
        result = phase_plan_completeness(
            phase_summaries={"primary": primary, "duplicate": duplicate},
            expected_phase_plan=[],
            expected_duplicate_count=1,
            acceptance_contract=SLO,
            max_retries=0,
            expected_logical_scope="logical_user_actions",
        )

        self.assertFalse(result["checks"]["duplicate_population"])


if __name__ == "__main__":
    unittest.main()
