from __future__ import annotations

import unittest

from tools.platform_load_acceptance import evaluate_acceptance


def latency(p50: float, p90: float, p95: float, p99: float) -> dict[str, float]:
    return {"p50_ms": p50, "p90_ms": p90, "p95_ms": p95, "p99_ms": p99}


SLO = {
    "kind": "slo",
    "accepted_request_latency": latency(250, 400, 600, 1000),
    "logical_latency": {"p95_ms": 600, "p99_ms": 1000},
    "logical_final_failure_percent": 0.5,
    "max_shed_percent": 0,
    "max_retry_amplification_percent": 0,
}


class LoadAcceptanceTests(unittest.TestCase):
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
                "final_failure_rate_percent": 60,
                "total_retries": 40,
                "successful_goodput_actions_per_second": 20,
                "accepted_request_latency": latency(100, 300, 500, 700),
            },
            raw_http_summary={
                "temporary_overload_rate_percent": 60,
                "unexpected_statuses": 0,
            },
            acceptance_contract={
                "kind": "stress",
                "accepted_request_latency": latency(1500, 3000, 5000, 8000),
                "max_shed_percent": 99.9,
                "max_retry_amplification_percent": 100,
                "max_postgres_connections": 52,
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
                    "postgres_established_connections": {"max": 40},
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
                        "final_failure_rate_percent": 0,
                        "end_to_end_latency": {"p95_ms": 100, "p99_ms": 200},
                        "accepted_request_latency": latency(100, 200, 300, 400),
                        "successful_goodput_actions_per_second": 20,
                    },
                    "raw_http": {"temporary_overload_rate_percent": 0},
                },
                "rate-40": {
                    "logical": {
                        "target_logical_actions_per_second": 40,
                        "actions": 40,
                        "final_failure_rate_percent": 20,
                        "end_to_end_latency": {"p95_ms": 700, "p99_ms": 1100},
                        "accepted_request_latency": latency(100, 200, 700, 1100),
                    "successful_goodput_actions_per_second": 32,
                    },
                    "raw_http": {"temporary_overload_rate_percent": 20},
                },
            },
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["slo_capacity_logical_actions_per_second"], 20)
        self.assertEqual(result["max_stable_goodput_actions_per_second"], 32)


if __name__ == "__main__":
    unittest.main()
