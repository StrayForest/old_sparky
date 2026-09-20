import unittest

from tools.platform_timeout_diagnostics import join_reports


class TimeoutDiagnosticsTests(unittest.TestCase):
    def test_join_keeps_bounded_origin_metrics_without_correlator_values(self) -> None:
        report = join_reports(
            {
                "source_git_sha": "a" * 40,
                "load_contract": {"profile_id": "authenticated-page-load-v1"},
                "overall": {
                    "timeout_diagnostics": [
                        {
                            "diagnostic_id": "secret-correlator",
                            "error_kind": "TimeoutError",
                            "method": "GET",
                            "path": "/tournaments/private-slug/workspace?invite=secret",
                            "elapsed_ms": 30_000.0,
                        }
                    ]
                },
            },
            {
                "server_ssr_observability": {
                    "timeout_diagnostics": {
                        "rows": [
                            {
                                "diagnostic_id": "secret-correlator",
                                "method": "GET",
                                "uri": "/tournaments/private-slug/workspace?token=secret",
                                "route_class": "tournament_workspace",
                                "status": 504,
                                "next": {
                                    "accepted": True,
                                    "upstream_completed": True,
                                    "request_time_ms": 30_000.0,
                                },
                                "ssr": {
                                    "started_observed": True,
                                    "stage_events": [{"stage": "secret-stage"}],
                                },
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
        self.assertEqual(report["summary"]["nginx_matches"], 1)
        row = report["rows"][0]
        self.assertEqual(row["classification"], "next_ssr_or_node_queue")
        self.assertEqual(row["origin"]["route_class"], "tournament_workspace")
        self.assertEqual(row["origin"]["next"]["request_time_ms"], 30_000.0)
        serialized = str(report)
        self.assertNotIn("secret-correlator", serialized)
        self.assertNotIn("private-slug", serialized)
        self.assertNotIn("invite=secret", serialized)
        self.assertNotIn("secret-stage", serialized)
        self.assertNotIn("diagnostic_id", serialized)
        self.assertNotIn("request_id", serialized)
        self.assertNotIn("path", row["client"])
        self.assertNotIn("uri", row["origin"])
        self.assertEqual(report["nginx_timeout_policy"]["proxy_read_timeout_seconds"], 30)

    def test_join_does_not_infer_order_from_removed_timestamps(self) -> None:
        report = join_reports(
            {
                "source_git_sha": "a" * 40,
                "load_contract": {"profile_id": "authenticated-page-load-v1"},
                "overall": {
                    "timeout_diagnostics": [
                        {
                            "diagnostic_id": "secret-correlator",
                            "error_kind": "TimeoutError",
                            "phase": "write_burst",
                            "method": "POST",
                            "route_class": "ready_vote",
                        }
                    ]
                },
            },
            {
                "server_ssr_observability": {
                    "timeout_diagnostics": {
                        "rows": [
                            {
                                "diagnostic_id": "secret-correlator",
                                "route_class": "ready_vote",
                                "method": "POST",
                                "status": 504,
                                "next": {
                                    "accepted": True,
                                    "upstream_completed": True,
                                    "request_time_ms": 500.0,
                                },
                                "ssr": {"started_observed": False},
                                "api": {},
                            }
                        ]
                    },
                    "event_loop": {"samples": 0, "samples_detail": []},
                },
                "system": {"timeline": []},
            },
        )

        self.assertEqual(
            report["summary"]["classifications"],
            {"next_accept_or_node_queue": 1},
        )
        self.assertEqual(report["summary"]["origin_requests_started_after_client_timeout"], 0)
        self.assertIsNone(report["rows"][0]["estimated_origin_request_start_delta_ms"])
        self.assertFalse(report["rows"][0]["origin_request_started_after_client_timeout"])

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
                            "phase": "page_load",
                            "method": "GET",
                            "route_class": "tournament_page",
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
