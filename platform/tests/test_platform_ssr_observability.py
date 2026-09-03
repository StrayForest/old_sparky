import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.platform_production_qa import (
    collect_nginx_access_records,
    parse_ssr_event_loop_line,
    parse_ssr_perf_line,
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
                "upstream_time": "0.110",
            },
        ]

        summary = summarize_ssr_observability(web_lines, records)
        serialized = json.dumps(summary)
        correlated = summary["correlated_html"]
        self.assertEqual(correlated["requests"], 1)
        self.assertEqual(correlated["upstream_time_ms"]["p50_ms"], 110.0)
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


if __name__ == "__main__":
    unittest.main()
