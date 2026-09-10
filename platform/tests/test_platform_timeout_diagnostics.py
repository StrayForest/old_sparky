import unittest

from tools.platform_timeout_diagnostics import join_reports


class TimeoutDiagnosticsTests(unittest.TestCase):
    def test_join_marks_origin_completion_after_client_timeout_and_keeps_nearest_samples(self) -> None:
        diagnostic_id = "tdiag-123-00001"
        report = join_reports(
            {
                "source_git_sha": "a" * 40,
                "load_contract": {"profile_id": "authenticated-page-load-v1"},
                "started_at": "2026-09-10T10:00:00+00:00",
                "finished_at": "2026-09-10T10:01:00+00:00",
                "overall": {
                    "timeout_diagnostics": [
                        {
                            "diagnostic_id": diagnostic_id,
                            "error_kind": "TimeoutError",
                            "started_at": "2026-09-10T10:00:00+00:00",
                            "exception_at": "2026-09-10T10:00:30+00:00",
                            "finished_at": "2026-09-10T10:00:30+00:00",
                        }
                    ]
                },
            },
            {
                "started_at": "2026-09-10T10:00:00+00:00",
                "finished_at": "2026-09-10T10:01:10+00:00",
                "system": {
                    "timeline": [
                        {
                            "timestamp": "2026-09-10T10:00:30+00:00",
                            "web_process": {"cpu_percent": 91.0},
                        }
                    ]
                },
                "server_ssr_observability": {
                    "event_loop": {
                        "samples": 1,
                        "samples_detail": [
                            {
                                "journal_timestamp": "2026-09-10T10:00:30+00:00",
                                "p95_ms": 18.0,
                                "cpu_pct": 91.0,
                            }
                        ],
                    },
                    "timeout_diagnostics": {
                        "rows": [
                            {
                                "diagnostic_id": diagnostic_id,
                                "nginx_recorded_at": "2026-09-10T10:00:31+00:00",
                                "next": {
                                    "accepted": True,
                                    "upstream_completed": True,
                                },
                                "ssr": {"started_observed": True},
                                "api": {
                                    "call_started_observed": False,
                                    "call_completed_observed": False,
                                },
                            }
                        ]
                    },
                },
            },
        )

        self.assertEqual(report["summary"]["next_accepted_matches"], 1)
        self.assertEqual(
            report["summary"]["upstream_completed_at_or_after_client_timeout"],
            1,
        )
        row = report["rows"][0]
        self.assertEqual(row["classification"], "next_ssr_or_node_queue")
        self.assertTrue(row["server_completed_at_or_after_client_timeout"])
        self.assertEqual(row["nearest_event_loop_sample"]["cpu_pct"], 91.0)
        self.assertEqual(report["nginx_timeout_policy"]["proxy_read_timeout_seconds"], 30)

    def test_join_keeps_missing_origin_as_unresolved_edge_or_client(self) -> None:
        report = join_reports(
            {
                "source_git_sha": "a" * 40,
                "load_contract": {"profile_id": "authenticated-page-load-v1"},
                "started_at": "2026-09-10T10:00:00+00:00",
                "finished_at": "2026-09-10T10:01:00+00:00",
                "overall": {
                    "timeout_diagnostics": [
                        {
                            "diagnostic_id": "tdiag-123-00001",
                            "error_kind": "TimeoutError",
                            "started_at": "2026-09-10T10:00:30+00:00",
                            "exception_at": "2026-09-10T10:01:00+00:00",
                        }
                    ]
                },
            },
            {
                "started_at": "2026-09-10T10:00:00+00:00",
                "finished_at": "2026-09-10T10:01:10+00:00",
                "server_ssr_observability": {
                    "timeout_diagnostics": {"rows": []},
                    "event_loop": {"samples": 0, "samples_detail": []},
                },
                "system": {"timeline": []},
            },
        )

        self.assertEqual(report["summary"]["client_timeout_errors"], 1)
        self.assertEqual(
            report["summary"]["classifications"],
            {"before_origin_observed": 1},
        )
        self.assertIsNone(report["rows"][0]["origin"])


if __name__ == "__main__":
    unittest.main()
