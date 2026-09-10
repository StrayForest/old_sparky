import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.platform_production_qa import (
    collect_nginx_access_records,
    parse_request_perf_start_line,
    parse_ssr_event_loop_line,
    parse_ssr_perf_line,
    parse_ssr_stream_line,
    summarize_ssr_observability,
)


class SsrObservabilityTests(unittest.TestCase):
    def test_ssr_stage_parser_keeps_only_safe_scalar_diagnostics(self) -> None:
        row = parse_ssr_perf_line(
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=tournament_workspace "
            "duration_ms=123.456 outcome=ok"
        )
        self.assertEqual(row["request_id"], "req-1")
        self.assertEqual(row["stage"], "tournament_workspace")
        self.assertEqual(row["duration_ms"], 123.456)
        self.assertIsNone(parse_ssr_perf_line("ssr_perf stage=missing-request duration_ms=1"))
        self.assertIsNone(
            parse_ssr_perf_line(
                "ssr_perf request_id=req-1 stage=bad duration_ms=NaN"
            )
        )
        self.assertEqual(
            parse_ssr_event_loop_line(
                "ssr_event_loop p50_ms=1.000 p95_ms=4.000 p99_ms=6.000 max_ms=8.000 "
                "mean_ms=2.000 elu=0.910000 cpu_pct=87.500 gc_count=2 gc_duration_ms=3.250"
            )["p95_ms"],
            4.0,
        )
        self.assertIsNone(
            parse_ssr_event_loop_line(
                "ssr_event_loop p50_ms=1.000 p95_ms=NaN"
            )
        )
        event_loop = parse_ssr_event_loop_line(
            "ssr_event_loop p50_ms=1.000 p95_ms=4.000 p99_ms=6.000 max_ms=8.000 "
            "mean_ms=2.000 elu=0.910000 cpu_pct=87.500 gc_count=2 gc_duration_ms=3.250"
        )
        self.assertEqual(event_loop["p99_ms"], 6.0)
        self.assertEqual(event_loop["elu"], 0.91)
        self.assertEqual(event_loop["cpu_pct"], 87.5)
        self.assertEqual(event_loop["gc_count"], 2.0)
        stream = parse_ssr_stream_line(
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=first_body_write_attempt "
            "elapsed_ms=123 status=200 active_requests=7 writable_finished=0 write_count=1 "
            "body_bytes=42 response_error=0"
        )
        self.assertEqual(stream["elapsed_ms"], 123.0)
        self.assertEqual(stream["status"], 200)
        self.assertEqual(stream["active_requests"], 7)
        self.assertEqual(stream["writable_finished"], 0)
        self.assertEqual(stream["write_count"], 1)
        self.assertEqual(stream["body_bytes"], 42)
        self.assertIsNone(parse_ssr_stream_line("ssr_stream stage=missing-request elapsed_ms=1"))
        self.assertIsNone(
            parse_ssr_stream_line(
                "ssr_stream request_id=req-1 stage=bad elapsed_ms=Infinity"
            )
        )

        self.assertEqual(
            parse_request_perf_start_line(
                "2026-09-10T10:00:01.123456+00:00 api[1]: "
                "request_perf_start request_id=tdiag-123-00001 "
                "diagnostic_id=tdiag-123-00001 method=GET path=/api/v1/auth/bootstrap"
            )["diagnostic_id"],
            "tdiag-123-00001",
        )

    def test_ssr_summary_correlates_sampled_stages_without_serializing_ids_or_uris(self) -> None:
        web_lines = [
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=auth_bootstrap_fetch duration_ms=12.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=tournament_workspace duration_ms=80.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=tournament_detail_data_ready duration_ms=90.000 outcome=ok",
            "ssr_event_loop p50_ms=1.000 p95_ms=4.000 max_ms=8.000 mean_ms=2.000",
            "ssr_event_loop p50_ms=1.000 p95_ms=4.000 p99_ms=6.000 max_ms=8.000 "
            "mean_ms=2.000 elu=0.910000 cpu_pct=87.500 gc_count=2 gc_duration_ms=3.250",
        ]
        records = [
            {
                "time": "2026-09-03T10:00:01+00:00",
                "request_id": "req-1",
                "method": "GET",
                "uri": "/tournaments/private-fixture-slug",
                "status": 200,
                "request_time": "0.120",
                "upstream_connect_time": "0.002",
                "upstream_header_time": "0.050",
                "upstream_time": "0.110",
            },
        ]

        summary = summarize_ssr_observability(web_lines, records)
        serialized = json.dumps(summary)
        correlated = summary["correlated_html"]
        self.assertEqual(correlated["requests"], 1)
        self.assertEqual(summary["event_loop"]["p99_ms"]["p95_ms"], 6.0)
        self.assertEqual(summary["event_loop"]["elu"]["p95"], 0.91)
        self.assertEqual(summary["event_loop"]["cpu_pct"]["p95"], 87.5)
        self.assertEqual(summary["event_loop"]["gc_count"]["p95"], 2.0)
        self.assertEqual(correlated["upstream_time_ms"]["p50_ms"], 110.0)
        self.assertEqual(correlated["upstream_connect_time_ms"]["p50_ms"], 2.0)
        self.assertEqual(correlated["upstream_header_time_ms"]["p50_ms"], 50.0)
        self.assertEqual(
            correlated["stage_ms"]["tournament_workspace"]["p50_ms"],
            80.0,
        )
        self.assertEqual(
            correlated["unattributed_upstream_after_data_ms"]["p50_ms"],
            20.0,
        )
        self.assertNotIn("private-fixture-slug", serialized)
        self.assertNotIn("req-1", serialized)

    def test_ssr_summary_includes_opt_in_memory_telemetry(self) -> None:
        summary = summarize_ssr_observability(
            [
                "ssr_event_loop pid=1234 p50_ms=1.000 p95_ms=4.000 p99_ms=6.000 "
                "max_ms=8.000 mean_ms=2.000 elu=0.910000 cpu_pct=87.500 "
                "gc_count=2 gc_duration_ms=3.250 rss_bytes=100000 "
                "heap_total_bytes=80000 heap_used_bytes=50000 "
                "external_bytes=25000 array_buffers_bytes=12000",
            ],
            [],
        )

        self.assertEqual(
            summary["event_loop"]["memory"]["rss_bytes"]["p95"],
            100000.0,
        )
        self.assertEqual(
            summary["event_loop"]["memory"]["array_buffers_bytes"]["p95"],
            12000.0,
        )
        self.assertEqual(
            summary["event_loop"]["by_pid"]["1234"]["memory"]["rss_bytes"]["p95"],
            100000.0,
        )

    def test_nginx_collection_filters_records_to_requested_window(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "platform-access.log"
            path.write_text(
                "{\"time\":\"2026-09-03T10:00:01+00:00\",\"status\":200}\n"
                "{\"time\":\"2026-09-03T10:00:10+00:00\",\"status\":200}\n",
                encoding="utf-8",
            )
            records = collect_nginx_access_records(
                datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC),
                datetime(2026, 9, 3, 10, 0, 5, tzinfo=UTC),
                log_path=path,
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], 200)

    def test_ssr_summary_ignores_malformed_status(self) -> None:
        records = [
            {
                "time": "2026-09-03T10:00:01+00:00",
                "method": "GET",
                "uri": "/tournaments/fixture",
                "status": "not-a-status",
            },
        ]
        summary = summarize_ssr_observability([], records)
        self.assertEqual(summary["nginx_html"]["requests"], 0)

    def test_ssr_summary_retains_correlated_timeline_and_api_metrics(self) -> None:
        web_lines = [
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=http_request_start "
            "start_ms=-12.000 end_ms=-12.000 duration_ms=0.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=proxy_start "
            "start_ms=-7.000 end_ms=-7.000 duration_ms=0.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=proxy_to_root_layout_start "
            "start_ms=-7.000 end_ms=0.000 duration_ms=7.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=request_to_proxy "
            "start_ms=-12.000 end_ms=-7.000 duration_ms=5.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=root_layout_start "
            "start_ms=0.000 end_ms=0.000 duration_ms=0.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=root_layout "
            "start_ms=0.000 end_ms=4.000 duration_ms=4.000 outcome=ok",
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=response_stream_start "
            "elapsed_ms=21 status=200 writable_finished=0 write_count=0 body_bytes=0 response_error=0",
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=first_body_write_attempt "
            "elapsed_ms=23 status=200 writable_finished=0 write_count=1 body_bytes=42 response_error=0",
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=response_finish "
            "elapsed_ms=25 status=200 writable_finished=1 write_count=2 body_bytes=48 response_error=0",
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=response_close "
            "elapsed_ms=25 status=200 writable_finished=1 write_count=2 body_bytes=48 response_error=0",
        ]
        api_lines = [
            "request_perf request_id=req-1 method=GET path=/api/v1/auth/bootstrap "
            "route=/api/v1/auth/bootstrap status=200 total_ms=18.5 sql_ms=2.5 "
            "sql_count=2 pool_checkout_wait_ms=1.5 pool_connection_hold_ms=3.5 "
            "compute_ms=4.5 response_bytes=120",
        ]
        summary = summarize_ssr_observability(
            web_lines,
            [
                {
                    "request_id": "req-1",
                    "method": "GET",
                    "uri": "/tournaments/fixture",
                    "status": 200,
                    "request_time": "0.050",
                    "upstream_header_time": "0.040",
                    "upstream_time": "0.045",
                }
            ],
            api_lines,
        )

        correlated = summary["correlated_html"]
        self.assertEqual(correlated["requests"], 1)
        self.assertEqual(correlated["stream_stage_presence"]["first_body_write_attempt"], 1)
        self.assertTrue(correlated["timeline"][0]["stream_clock_aligned"])
        timeline = correlated["timeline"][0]["timeline"]
        self.assertEqual(
            [event["stage"] for event in timeline],
            [
                "http_request_start",
                "request_to_proxy",
                "proxy_start",
                "proxy_to_root_layout_start",
                "root_layout_start",
                "root_layout",
                "response_stream_start",
                "first_body_write_attempt",
                "response_finish",
                "response_close",
            ],
        )
        self.assertEqual(timeline[7]["start_ms"], 11.0)
        self.assertEqual(summary["ssr_stream"]["integrity"]["response_finish_requests"], 1)
        self.assertEqual(summary["ssr_stream"]["integrity"]["close_without_finish_requests"], 0)
        self.assertEqual(summary["ssr_stream"]["integrity"]["body_bytes"]["p50_ms"], 48.0)
        self.assertEqual(
            correlated["timeline"][0]["api_request_perf"][0]["route_class"],
            "auth_bootstrap",
        )
        serialized = json.dumps(summary)
        self.assertNotIn("req-1", serialized)

    def test_ssr_summary_falls_back_to_cf_ray_for_direct_api_hop(self) -> None:
        summary = summarize_ssr_observability(
            [
                "ssr_perf request_id=req-1 cf_ray=ray-1 stage=http_request_start "
                "start_ms=-10.000 end_ms=-10.000 duration_ms=0.000 outcome=ok",
            ],
            [
                {
                    "request_id": "req-1",
                    "cf_ray": "ray-1",
                    "method": "GET",
                    "uri": "/tournaments/fixture",
                    "status": 200,
                    "request_time": "0.050",
                    "upstream_header_time": "0.040",
                    "upstream_time": "0.045",
                }
            ],
            [
                "request_perf request_id=api-generated method=GET "
                "path=/api/v1/auth/bootstrap route=/api/v1/auth/bootstrap "
                "status=200 cf_ray=ray-1 total_ms=18.5 sql_ms=2.5",
            ],
        )

        correlated = summary["correlated_html"]
        self.assertEqual(correlated["api_request_perf_join"]["api_rows"], 1)
        self.assertEqual(correlated["api_request_perf_join"]["matched_by_request_id"], 0)
        self.assertEqual(correlated["api_request_perf_join"]["matched_by_cf_ray"], 1)
        self.assertEqual(
            correlated["timeline"][0]["api_request_perf_correlation"],
            "cf_ray",
        )
        self.assertEqual(
            correlated["timeline"][0]["api_request_perf"][0]["route_class"],
            "auth_bootstrap",
        )

    def test_ssr_summary_joins_diagnostic_identity_to_nginx_and_api(self) -> None:
        diagnostic_id = "tdiag-123-00001"
        summary = summarize_ssr_observability(
            [
                f"ssr_perf request_id={diagnostic_id} cf_ray=ray-1 "
                "stage=root_layout duration_ms=4.000 outcome=ok",
            ],
            [
                {
                    "request_id": "nginx-owned-id",
                    "timeout_diagnostic_id": diagnostic_id,
                    "cf_ray": "ray-1",
                    "method": "GET",
                    "uri": "/tournaments/fixture",
                    "status": 200,
                    "request_time": "0.050",
                    "upstream_header_time": "0.040",
                    "upstream_time": "0.045",
                }
            ],
            [
                "request_perf request_id=tdiag-123-00001 method=GET "
                "path=/api/v1/auth/bootstrap route=/api/v1/auth/bootstrap "
                "status=200 total_ms=18.5 sql_ms=2.5",
            ],
        )

        correlated = summary["correlated_html"]
        self.assertEqual(correlated["requests"], 1)
        self.assertEqual(correlated["api_request_perf_join"]["matched_by_diagnostic_id"], 1)
        self.assertEqual(correlated["api_request_perf_join"]["matched_by_request_id"], 0)
        self.assertEqual(correlated["timeline"][0]["diagnostic_id"], diagnostic_id)
        self.assertEqual(correlated["timeline"][0]["api_request_perf_correlation"], "diagnostic_id")

    def test_ssr_summary_marks_close_without_finish_as_response_integrity_failure(self) -> None:
        summary = summarize_ssr_observability(
            [
                "ssr_perf request_id=req-2 cf_ray=ray-2 stage=http_request_start "
                "start_ms=-10.000 end_ms=-10.000 duration_ms=0.000 outcome=ok",
                "ssr_stream request_id=req-2 cf_ray=ray-2 stage=response_close "
                "elapsed_ms=10 status=200 writable_finished=0 write_count=1 "
                "body_bytes=4 response_error=1",
            ],
            [
                {
                    "request_id": "req-2",
                    "method": "GET",
                    "uri": "/tournaments/fixture",
                    "status": 200,
                    "request_time": "0.020",
                    "upstream_header_time": "0.010",
                    "upstream_time": "0.015",
                }
            ],
        )

        integrity = summary["ssr_stream"]["integrity"]
        self.assertEqual(integrity["response_error_requests"], 1)
        self.assertEqual(integrity["close_without_finish_requests"], 1)
        self.assertEqual(summary["correlated_html"]["timeline"][0]["timeline"][1]["writable_finished"], 0)

    def test_timeout_summary_joins_diagnostic_id_to_next_ssr_api_and_event_loop(self) -> None:
        diagnostic_id = "tdiag-123-00001"
        summary = summarize_ssr_observability(
            [
                f"2026-09-10T10:00:01.200000+00:00 web[1]: ssr_perf request_id={diagnostic_id} "
                "cf_ray=ray-1 stage=root_layout_start start_ms=0.000 end_ms=0.000 "
                "duration_ms=0.000 outcome=ok",
                f"2026-09-10T10:00:01.300000+00:00 web[1]: ssr_stream request_id={diagnostic_id} "
                "cf_ray=ray-1 stage=request_start elapsed_ms=2 status=200 active_requests=7 "
                "writable_finished=0 write_count=0 body_bytes=0 response_error=0",
                "2026-09-10T10:00:01.400000+00:00 web[1]: ssr_event_loop p50_ms=1.000 "
                "p95_ms=18.000 p99_ms=25.000 max_ms=30.000 mean_ms=2.000 "
                "elu=0.920000 cpu_pct=91.000 gc_count=1 gc_duration_ms=2.000",
            ],
            [
                {
                    "time": "2026-09-10T10:00:31+00:00",
                    "request_id": "nginx-1",
                    "timeout_diagnostic_id": diagnostic_id,
                    "cf_ray": "ray-1",
                    "method": "GET",
                    "uri": "/tournaments/fixture",
                    "status": 200,
                    "request_completion": "OK",
                    "request_time": "30.100",
                    "upstream_connect_time": "0.002",
                    "upstream_header_time": "30.000",
                    "upstream_time": "30.050",
                    "upstream_status": "200",
                    "upstream_addr": "127.0.0.1:3000",
                }
            ],
            [
                "2026-09-10T10:00:01.250000+00:00 api[1]: request_perf_start "
                f"request_id={diagnostic_id} diagnostic_id={diagnostic_id} method=GET "
                "path=/api/v1/auth/bootstrap",
                "2026-09-10T10:00:01.500000+00:00 api[1]: request_perf "
                f"request_id={diagnostic_id} diagnostic_id={diagnostic_id} method=GET "
                "path=/api/v1/auth/bootstrap route=/api/v1/auth/bootstrap status=200 "
                "total_ms=18.5 sql_ms=2.5 sql_count=2 pool_checkout_wait_ms=1.5 "
                "pool_connection_hold_ms=3.5 compute_ms=4.5 response_bytes=120",
            ],
            timeout_diagnostic_ids={diagnostic_id},
        )

        timeout = summary["timeout_diagnostics"]
        self.assertEqual(timeout["requested_ids"], 1)
        self.assertEqual(timeout["nginx_records"], 1)
        row = timeout["rows"][0]
        self.assertTrue(row["next"]["accepted"])
        self.assertTrue(row["next"]["upstream_completed"])
        self.assertTrue(row["ssr"]["started_observed"])
        self.assertEqual(row["ssr"]["stream_events"][0]["active_requests"], 7)
        self.assertTrue(row["api"]["call_started_observed"])
        self.assertTrue(row["api"]["call_completed_observed"])
        self.assertEqual(summary["event_loop"]["samples_detail"][0]["cpu_pct"], 91.0)

    def test_nginx_numeric_request_time_is_reported(self) -> None:
        summary = summarize_ssr_observability(
            [],
            [
                {
                    "method": "GET",
                    "uri": "/tournaments/fixture",
                    "status": 200,
                    "request_time": 1.25,
                    "upstream_time": "1.10",
                }
            ],
        )

        self.assertEqual(summary["nginx_html"]["request_time_ms"]["p50_ms"], 1250.0)

    def test_nginx_api_summary_uses_safe_route_classes_and_statuses(self) -> None:
        summary = summarize_ssr_observability(
            [],
            [
                {
                    "request_id": "request-secret",
                    "cf_ray": "ray-secret",
                    "method": "GET",
                    "uri": "/api/v1/auth/bootstrap?token=secret",
                    "status": 200,
                    "request_time": "0.800",
                    "upstream_connect_time": "0.002",
                    "upstream_header_time": "0.400",
                    "upstream_time": "0.700",
                },
                {
                    "request_id": "request-secret-2",
                    "cf_ray": "-",
                    "method": "POST",
                    "uri": "/api/v1/tournaments/private-fixture/deadlock/ready-check/vote",
                    "status": 503,
                    "request_time": "0.010",
                    "upstream_connect_time": "0.001",
                    "upstream_header_time": "0.003",
                    "upstream_time": "0.009",
                },
                {
                    "method": "POST",
                    "uri": "/api/v1/tournaments/private-fixture/deadlock/ready-check/vote",
                    "status": 522,
                    "request_time": "30.000",
                    "upstream_connect_time": "5.000",
                    "upstream_header_time": "5.000",
                    "upstream_time": "30.000",
                },
            ],
        )

        api = summary["nginx_api"]
        self.assertEqual(api["requests"], 3)
        self.assertEqual(
            api["by_method_route"]["GET auth_bootstrap"]["request_time_ms"]["p50_ms"],
            800.0,
        )
        vote = api["by_method_route"]["POST ready_vote"]
        self.assertEqual(vote["statuses"], {"503": 1, "522": 1})
        self.assertEqual(vote["request_time_ms"]["p99_ms"], 29700.1)
        self.assertEqual(vote["upstream_connect_time_ms"]["p99_ms"], 4950.01)
        self.assertEqual(vote["upstream_header_time_ms"]["p99_ms"], 4950.03)
        self.assertEqual(vote["cf_ray_present"], 0)
        serialized = json.dumps(summary)
        self.assertNotIn("private-fixture", serialized)
        self.assertNotIn("request-secret", serialized)


if __name__ == "__main__":
    unittest.main()
