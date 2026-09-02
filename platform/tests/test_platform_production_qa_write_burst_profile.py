import unittest

from tools.platform_production_qa import (
    HttpMetricsRecorder,
    HttpSample,
    SystemSampler,
    _attach_server_diagnostic_sample,
    burst_offsets,
    follow_up_read_counts,
    evaluate_write_burst_profiles,
    is_compact_mutation_response,
    summarize_bottleneck_evidence,
    summarize_request_perf_logs,
)


class ProductionQaWriteBurstProfileTests(unittest.TestCase):
    def test_system_summary_keeps_sample_window_when_postgres_has_active_queries(self) -> None:
        sampler = SystemSampler.__new__(SystemSampler)
        sampler.interval_seconds = 1.0
        sampler.samples = [
            {
                "cpu_per_core_percent": {"cpu0": 10.0},
                "memory_used_bytes": 100,
                "memory_total_bytes": 200,
                "swap_used_bytes": 0,
                "swap_total_bytes": 0,
                "load_average": {"1m": 0.1, "5m": 0.1, "15m": 0.1},
                "nginx_connections": {"established": 1},
                "postgres_connections": {"established": 2},
                "redis_connections": {"established": 3},
                "gunicorn": {"workers": 2},
                "postgres_cpu_percent": 4.0,
                "processes": {
                    "api": {
                        "process_count": 2,
                        "cpu_percent": 10.0,
                        "rss_bytes": 100,
                        "read_bytes_per_second": 0,
                        "write_bytes_per_second": 0,
                    }
                },
                "postgres_waits": {
                    "lock_waiters": 0,
                    "waiting_backends": 0,
                    "ungranted_locks": 0,
                    "max_waiting_query_ms": 10,
                    "max_lock_waiting_query_ms": 0,
                    "active_query_samples": [{"query_age_ms": 10}],
                },
                "celery_backlog": {
                    "deadlock-platform-high": 0,
                    "deadlock-platform-default": 0,
                    "deadlock-platform-low": 0,
                },
            }
        ]

        result = sampler.summary()

        self.assertEqual(result["samples"], 1)
        self.assertEqual(result["gunicorn_workers"]["last"], 2)
        self.assertEqual(
            result["postgres_waits"]["active_query_samples"][0]["query_age_ms"],
            10,
        )

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

        self.assertEqual(summary["scope"], "full_population")
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
                "request_ms=25.00 db_sql_ms=8.00 sql_ms=8.00 sql_count=3 max_sql_ms=5.00 compute_ms=0.00 "
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

    def test_request_perf_summary_exposes_ready_vote_spans(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=POST path=/api/v1/tournaments/demo/deadlock/ready-check/vote "
                "route=/{slug}/deadlock/ready-check/vote status=200 total_ms=40.00 sql_ms=20.00 "
                "sql_count=3 max_sql_ms=10.00 compute_ms=2.00 compute_blocks=1 response_bytes=180 "
                "pool_wait_ms=3.00 qa_phase=write_ready_burst_5s "
                "ready_vote_auth_ms=4.00 ready_vote_checkout_count=1 ready_vote_checkout_ms=5.00 "
                "ready_vote_admission_inflight=3 ready_vote_admission_limit=4 "
                "ready_vote_admission_wait_ms=1.00 ready_vote_admitted_total=12 "
                "ready_vote_shed_total=2 ready_vote_controller_limit_changes=3 "
                "ready_vote_controller_state=pressure "
                "ready_vote_cpu_pressure=81.50 ready_vote_pool_wait_ms=3.00 "
                "ready_vote_cpu_monitor_sample_ms=0.12 ready_vote_cpu_monitor_samples=20 "
                "ready_vote_preflight_ms=6.00 ready_vote_upsert_ms=7.00 "
                "ready_vote_commit_ms=2.00 ready_vote_response_ms=0.10",
            ],
            tournament_slug="demo",
        )

        ready_vote = summary["by_route"]["/{slug}/deadlock/ready-check/vote"]["ready_vote"]
        self.assertEqual(summary["scope"]["kind"], "diagnostic_sample")
        self.assertEqual(ready_vote["ready_vote_auth_ms"]["avg_ms"], 4.0)
        self.assertEqual(ready_vote["ready_vote_checkout_count"]["avg_ms"], 1.0)
        self.assertEqual(ready_vote["ready_vote_checkout_ms"]["avg_ms"], 5.0)
        self.assertEqual(ready_vote["ready_vote_admission_limit"]["avg_ms"], 4.0)
        self.assertEqual(ready_vote["ready_vote_cpu_pressure"]["avg_ms"], 81.5)
        self.assertEqual(ready_vote["ready_vote_cpu_monitor_samples"]["avg_ms"], 20.0)
        self.assertEqual(
            summary["by_route"]["/{slug}/deadlock/ready-check/vote"]
            ["ready_vote_controller_state_counts"],
            {"pressure": 1},
        )
        self.assertEqual(ready_vote["ready_vote_commit_ms"]["p95_ms"], 2.0)
        self.assertNotIn("ready_vote_counter_ms", ready_vote)
        self.assertNotIn("ready_vote_db_checkout_ms", ready_vote)

    def test_external_http_summary_marks_full_population(self) -> None:
        recorder = HttpMetricsRecorder()
        self.assertEqual(recorder.summary()["scope"], "full_population")

    def test_nested_write_burst_server_reports_require_diagnostic_scope(self) -> None:
        server_by_phase = {
            "write_ready_burst_5s": {"requests": 2, "overall": {"count": 2}},
        }
        write_burst = {
            "profiles": [{"phase": "write_ready_burst_5s"}],
        }
        _attach_server_diagnostic_sample(write_burst, server_by_phase)

        self.assertEqual(write_burst["server_by_phase"]["scope"], "diagnostic_sample")
        self.assertEqual(write_burst["server_by_phase"]["by_phase"], server_by_phase)
        self.assertEqual(write_burst["profiles"][0]["server"]["scope"], "diagnostic_sample")
        self.assertEqual(write_burst["profiles"][0]["server"]["summary"], server_by_phase["write_ready_burst_5s"])

    def test_request_perf_summary_keeps_workspace_pressure_in_route_breakdown(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=GET path=/api/v1/tournaments/demo/workspace "
                "route=/tournaments/{slug}/workspace status=200 total_ms=125.00 sql_ms=80.00 "
                "sql_count=6 max_sql_ms=30.00 compute_ms=5.00 compute_blocks=1 "
                "workspace_auth_ms=3.00 workspace_tournament_base_ms=20.00 "
                "workspace_media_ms=4.00 workspace_access_ms=8.00 "
                "workspace_invite_ms=5.00 workspace_bracket_ms=10.00 "
                "workspace_ready_check_ms=12.00 workspace_serialization_ms=2.00 "
                "workspace_etag_ms=0.10 response_bytes=640 qa_phase=- pool_wait_ms=12.00",
                "request_perf request_id=two method=GET path=/api/v1/tournaments/demo/workspace "
                "route=/tournaments/{slug}/workspace status=200 total_ms=250.00 sql_ms=160.00 "
                "sql_count=6 max_sql_ms=40.00 compute_ms=8.00 compute_blocks=1 "
                "workspace_auth_ms=4.00 workspace_tournament_base_ms=30.00 "
                "workspace_media_ms=5.00 workspace_access_ms=9.00 "
                "workspace_invite_ms=6.00 workspace_bracket_ms=11.00 "
                "workspace_ready_check_ms=13.00 workspace_serialization_ms=3.00 "
                "workspace_etag_ms=0.20 response_bytes=640 qa_phase=- pool_wait_ms=20.00",
            ],
            tournament_slug=None,
        )

        workspace = summary["by_method_route"]["GET /tournaments/{slug}/workspace"]
        self.assertEqual(workspace["requests"], 2)
        self.assertEqual(workspace["total"]["p95_ms"], 243.75)
        self.assertEqual(workspace["avg_sql_queries_per_request"], 6.0)
        self.assertEqual(workspace["pool_checkout_wait_ms"]["p99_ms"], 19.92)
        self.assertEqual(workspace["workspace"]["workspace_auth_ms"]["avg_ms"], 3.5)
        self.assertEqual(workspace["workspace"]["workspace_bracket_ms"]["p95_ms"], 10.95)

    def test_request_perf_summary_exposes_connection_hold_and_read_admission(self) -> None:
        summary = summarize_request_perf_logs(
            [
                "request_perf request_id=one method=GET path=/api/v1/users/me "
                "route=/users/me status=200 total_ms=900.00 request_ms=900.00 "
                "sql_ms=229.00 db_sql_ms=229.00 sql_count=5 max_sql_ms=100.00 "
                "compute_ms=10.00 compute_blocks=1 response_bytes=420 "
                "pool_checkout_wait_ms=100.00 pool_connection_hold_ms=900.00 "
                "pool_connection_hold_count=1 authenticated_read_admission_wait_ms=2.00 "
                "qa_phase=scale_external_read_mix_c64 pool_wait_ms=100.00"
            ],
            tournament_slug=None,
        )

        route = summary["by_route"]["/users/me"]
        self.assertEqual(summary["pool_connection_hold_ms"]["avg_ms"], 900.0)
        self.assertEqual(route["pool_connection_hold_ms"]["avg_ms"], 900.0)
        self.assertEqual(summary["authenticated_read_admission_wait_ms"]["avg_ms"], 2.0)

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
