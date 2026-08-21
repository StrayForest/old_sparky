import unittest

from tools.platform_production_qa import (
    HttpMetricsRecorder,
    HttpSample,
    burst_offsets,
    follow_up_read_counts,
    evaluate_write_burst_profiles,
    is_compact_mutation_response,
    summarize_bottleneck_evidence,
    summarize_request_perf_logs,
)


class ProductionQaWriteBurstProfileTests(unittest.TestCase):
    def test_burst_offsets_are_even_and_do_not_exceed_window(self) -> None:
        offsets = burst_offsets(count=5, spread_seconds=10)

        self.assertEqual(offsets, [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(burst_offsets(count=0, spread_seconds=10), [])
        self.assertEqual(burst_offsets(count=2, spread_seconds=0), [0.0, 0.0])

    def test_compact_mutation_response_rejects_nested_payloads(self) -> None:
        self.assertTrue(
            is_compact_mutation_response(
                {"id": "participant", "status": "registered", "changed": True},
                max_fields=3,
            )
        )
        self.assertFalse(
            is_compact_mutation_response(
                {"id": "participant", "workspace": {"participants": []}},
                max_fields=3,
            )
        )

    def test_http_summary_can_isolate_one_burst_phase(self) -> None:
        recorder = HttpMetricsRecorder()
        recorder.record(
            phase="write_join_burst_10s",
            method="POST",
            path="/tournaments/{slug}/join",
            status_code=201,
            elapsed_seconds=0.1,
            ok=True,
            started_at=1.0,
            finished_at=1.1,
            response_bytes=200,
        )
        recorder.record(
            phase="write_join_burst_30s",
            method="POST",
            path="/tournaments/{slug}/join",
            status_code=201,
            elapsed_seconds=0.2,
            ok=True,
            started_at=2.0,
            finished_at=2.2,
            response_bytes=220,
        )

        summary = recorder.summary(phases={"write_join_burst_10s"})

        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["overall"]["p95_ms"], 100.0)
        self.assertEqual(summary["overall"]["response_bytes"]["max_bytes"], 200)

    def test_follow_up_counts_only_requested_phases(self) -> None:
        samples = [
            HttpSample(
                phase="write_ready_burst_5s_followup",
                method="GET",
                path="/tournaments/{slug}/deadlock/ready-check",
                status_code=200,
                elapsed_ms=10,
                ok=True,
                started_at=1,
                finished_at=1.01,
                response_bytes=100,
            ),
            HttpSample(
                phase="write_ready_setup",
                method="GET",
                path="/tournaments/{slug}",
                status_code=200,
                elapsed_ms=10,
                ok=True,
                started_at=2,
                finished_at=2.01,
                response_bytes=100,
            ),
        ]

        counts = follow_up_read_counts(
            samples,
            phase_prefix="write_",
            phase_token="_followup",
        )

        self.assertEqual(
            counts,
            {"GET /tournaments/{slug}/deadlock/ready-check": 1},
        )

    def test_request_perf_summary_includes_method_and_response_bytes(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=POST path=/api/v1/tournaments/demo/join "
                "route=/api/v1/tournaments/{slug}/join status=201 total_ms=25.00 "
                "sql_ms=8.00 sql_count=3 max_sql_ms=5.00 compute_ms=0.00 "
                "compute_blocks=0 response_bytes=320"
                " qa_phase=write_join_burst_10s"
            ],
            tournament_slug="demo",
        )

        route = summary["by_method_route"]["POST /api/v1/tournaments/{slug}/join"]
        self.assertEqual(route["avg_sql_queries_per_request"], 3.0)
        self.assertEqual(route["response_bytes"]["max_bytes"], 320)
        self.assertEqual(
            summary["by_qa_phase"]["write_join_burst_10s"]["requests"],
            1,
        )

    def test_write_burst_acceptance_separates_target_budget(self) -> None:
        acceptance = evaluate_write_burst_profiles(
            [
                {
                    "name": "mixed",
                    "http": {"overall": {"p95_ms": 105, "p99_ms": 175}},
                    "system": {
                        "samples": 10,
                        "cpu_per_core": {
                            "cpu0": {"avg_percent": 32},
                            "cpu1": {"avg_percent": 29},
                        },
                        "postgres_waits": {
                            "max_lock_waiters": 0,
                            "max_lock_waiting_query_ms": 0,
                        },
                    },
                }
            ]
        )

        self.assertTrue(acceptance["healthy"])
        self.assertEqual(acceptance["failures"], [])

    def test_transient_cpu_peak_is_not_sustained_saturation(self) -> None:
        summary = summarize_bottleneck_evidence(
            http_summary={"by_phase": {}, "by_route": {}},
            server_summary={"by_route": {}},
            system_summary={
                "cpu_per_core": {
                    "cpu0": {"avg_percent": 25, "max_percent": 100},
                    "cpu1": {"avg_percent": 24, "max_percent": 100},
                },
                "load_average_1m": {"max": 1.5},
                "postgres_established_connections": {"max": 40},
                "postgres_waits": {
                    "max_lock_waiters": 1,
                    "max_ungranted_locks": 0,
                    "max_lock_waiting_query_ms": 60,
                    "max_waiting_backends": 0,
                    "max_waiting_query_ms": 0,
                },
                "processes": {},
            },
        )

        self.assertFalse(summary["resource_flags"]["cpu_sustained_saturation"])
        self.assertTrue(summary["resource_flags"]["cpu_peak_saturation"])
        self.assertTrue(summary["resource_flags"]["postgres_lock_wait_observed"])
        self.assertFalse(summary["resource_flags"]["postgres_lock_contention"])
        self.assertNotIn(
            "cpu_or_python_serialization_saturation",
            summary["likely_bottleneck_classes"],
        )


if __name__ == "__main__":
    unittest.main()
