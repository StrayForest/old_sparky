import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.platform_production_qa import (
    collect_nginx_access_records,
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
        self.assertEqual(
            parse_ssr_event_loop_line(
                "ssr_event_loop p50_ms=1.000 p95_ms=4.000 max_ms=8.000 mean_ms=2.000"
            )["p95_ms"],
            4.0,
        )
        stream = parse_ssr_stream_line(
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=first_chunk_emitted "
            "elapsed_ms=123 status=200"
        )
        self.assertEqual(stream["elapsed_ms"], 123.0)
        self.assertEqual(stream["status"], 200)
        self.assertIsNone(parse_ssr_stream_line("ssr_stream stage=missing-request elapsed_ms=1"))

    def test_ssr_summary_correlates_sampled_stages_without_serializing_ids_or_uris(self) -> None:
        web_lines = [
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=auth_bootstrap_fetch duration_ms=12.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=tournament_workspace duration_ms=80.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=tournament_detail_data_ready duration_ms=90.000 outcome=ok",
            "ssr_event_loop p50_ms=1.000 p95_ms=4.000 max_ms=8.000 mean_ms=2.000",
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
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=proxy_to_root_layout_start "
            "start_ms=-12.000 end_ms=0.000 duration_ms=12.000 outcome=ok",
            "ssr_perf request_id=req-1 cf_ray=ray-1 stage=root_layout "
            "start_ms=0.000 end_ms=20.000 duration_ms=20.000 outcome=ok",
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=response_stream_start "
            "elapsed_ms=40 status=200",
            "ssr_stream request_id=req-1 cf_ray=ray-1 stage=first_chunk_emitted "
            "elapsed_ms=41 status=200",
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
        self.assertEqual(correlated["stream_stage_presence"]["first_chunk_emitted"], 1)
        timeline = correlated["timeline"][0]["timeline"]
        self.assertEqual(
            [event["stage"] for event in timeline],
            ["proxy_to_root_layout_start", "root_layout", "response_stream_start", "first_chunk_emitted"],
        )
        self.assertEqual(timeline[2]["start_ms"], 28.0)
        self.assertEqual(
            correlated["timeline"][0]["api_request_perf"][0]["route_class"],
            "auth_bootstrap",
        )
        serialized = json.dumps(summary)
        self.assertNotIn("req-1", serialized)

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
