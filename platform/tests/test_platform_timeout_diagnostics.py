import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.platform_production_qa import summarize_ssr_observability
from tools.platform_timeout_diagnostics import join_reports


class TimeoutDiagnosticsTests(unittest.TestCase):
    def test_join_keeps_bounded_origin_metrics_without_correlator_values(self) -> None:
        first_id = "tdiag-123-00001"
        second_id = "tdiag-123-00002"
        origin = summarize_ssr_observability(
            [],
            [
                {
                    "timeout_diagnostic_id": second_id,
                    "method": "GET",
                    "uri": "/tournaments/other-slug",
                    "status": 504,
                    "request_completion": "timeout",
                    "upstream_addr": "127.0.0.1:3000",
                    "upstream_status": "504",
                    "request_time": "30.000",
                    "upstream_connect_time": "0.002",
                    "upstream_header_time": "30.000",
                    "upstream_time": "30.000",
                },
                {
                    "timeout_diagnostic_id": first_id,
                    "method": "GET",
                    "uri": "/tournaments/private-slug/workspace?invite=secret",
                    "status": 499,
                    "request_completion": "aborted",
                    "upstream_addr": "-",
                    "upstream_status": "-",
                    "request_time": "30.001",
                    "upstream_connect_time": "-",
                    "upstream_header_time": "-",
                    "upstream_time": "-",
                },
                {
                    "timeout_diagnostic_id": "tdiag-123-00003",
                    "method": "GET",
                    "uri": "/tournaments/sibling-private-slug?invite=sibling-secret",
                    "status": 504,
                    "request_completion": "timeout",
                    "upstream_addr": "127.0.0.1:3000",
                    "upstream_status": "504",
                    "request_time": "30.002",
                    "upstream_connect_time": "0.003",
                    "upstream_header_time": "30.002",
                    "upstream_time": "30.002",
                },
            ],
            timeout_diagnostic_ids={first_id, second_id},
        )
        self.assertEqual(
            [row["diagnostic_id"] for row in origin["timeout_diagnostics"]["rows"]],
            [second_id, first_id],
        )
        report = join_reports(
            {
                "load_contract": {"profile_id": "authenticated-page-load-v1"},
                "overall": {
                    "timeout_diagnostics": [
                        {
                            "diagnostic_id": first_id,
                            "error_kind": "TimeoutError",
                            "method": "GET",
                            "path": "/tournaments/private-slug/workspace?invite=secret",
                            "elapsed_ms": 30_000.0,
                        },
                        {
                            "diagnostic_id": second_id,
                            "error_kind": "TimeoutError",
                            "method": "GET",
                            "path": "/tournaments/other-slug",
                            "elapsed_ms": 30_000.0,
                        },
                    ]
                },
            },
            {"server_ssr_observability": origin},
        )

        self.assertEqual(report["summary"]["next_accepted_matches"], 1)
        self.assertEqual(report["summary"]["nginx_matches"], 2)
        first, second = report["rows"]
        self.assertEqual(first["origin"]["route_class"], "tournament_workspace")
        self.assertEqual(first["origin"]["status"], 499)
        self.assertFalse(first["origin"]["next"]["accepted"])
        self.assertEqual(second["origin"]["route_class"], "tournament_page")
        self.assertEqual(second["origin"]["status"], 504)
        self.assertTrue(second["origin"]["next"]["accepted"])
        self.assertEqual(second["origin"]["next"]["request_time_ms"], 30_000.0)
        serialized = str(report)
        self.assertNotIn(first_id, serialized)
        self.assertNotIn(second_id, serialized)
        self.assertNotIn("private-slug", serialized)
        self.assertNotIn("invite=secret", serialized)
        self.assertNotIn("diagnostic_id", serialized)
        self.assertNotIn("request_id", serialized)
        self.assertNotIn("path", first["client"])
        self.assertNotIn("uri", first["origin"])
        self.assertEqual(report["nginx_timeout_policy"]["proxy_read_timeout_seconds"], 30)
        self.assertEqual(len(origin["timeout_diagnostics"]["rows"]), 2)

        sanitizer_cli = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "platform_evidence_sanitizer.py"
        )
        artifacts = {
            "timeout_diagnostics": {
                **report,
                "started_at": "2030-01-02T03:04:05Z",
                "finished_at": "2030-01-02T03:04:35Z",
                "sibling_ssr": {"request_id": "sibling-request-secret"},
            },
            "server_observability": {
                "server_ssr_observability": {
                    **origin,
                    "sibling_ssr": {"request_id": "sibling-request-secret"},
                },
                "started_at": "2030-01-02T03:04:05Z",
                "finished_at": "2030-01-02T03:04:35Z",
            },
        }
        with tempfile.TemporaryDirectory(prefix="timeout-diag-public-") as temporary_dir:
            for artifact_type, payload in artifacts.items():
                with self.subTest(public_artifact=artifact_type):
                    input_path = Path(temporary_dir) / f"{artifact_type}.private.json"
                    output_path = Path(temporary_dir) / f"{artifact_type}.public.json"
                    input_path.write_text(json.dumps(payload), encoding="utf-8")
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(sanitizer_cli),
                            "--input",
                            str(input_path),
                            "--output",
                            str(output_path),
                            "--json",
                            "--artifact-type",
                            artifact_type,
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    public_output = output_path.read_text(encoding="utf-8")
                    for private_value in (
                        first_id,
                        second_id,
                        "tdiag-123-00003",
                        "private-slug",
                        "sibling-private-slug",
                        "other-slug",
                        "invite=secret",
                        "invite=sibling-secret",
                        "sibling-request-secret",
                        "2030-01-02T03:04:05Z",
                        "2030-01-02T03:04:35Z",
                    ):
                        self.assertNotIn(private_value, public_output)
                    if artifact_type == "timeout_diagnostics":
                        self.assertNotIn("diagnostic_id", public_output)

    def test_join_does_not_infer_order_from_removed_timestamps(self) -> None:
        report = join_reports(
            {
                "source_git_sha": "a" * 40,
                "load_contract": {"profile_id": "authenticated-page-load-v1"},
                "overall": {
                    "timeout_diagnostics": [
                        {
                            "diagnostic_id": "tdiag-123-00001",
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
                                "diagnostic_id": "tdiag-123-00001",
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
        client_id = "tdiag-456-00001"
        valid_origin = {
            "diagnostic_id": client_id,
            "route_class": "tournament_page",
            "method": "GET",
            "status": 504,
            "next": {"accepted": True, "upstream_completed": False},
            "ssr": {"started_observed": True},
            "api": {"call_started_observed": True},
        }
        client_row = {
            "diagnostic_id": client_id,
            "error_class": "timeout",
            "phase": "authenticated_page_load",
            "method": "GET",
            "route_class": "tournament_page",
        }

        def joined(origin_rows, client_rows=None):
            return join_reports(
                {
                    "overall": {"timeout_diagnostics": client_rows or [client_row]},
                },
                {
                    "server_ssr_observability": {
                        "timeout_diagnostics": {"rows": origin_rows}
                    }
                },
            )

        exact = joined([valid_origin])
        self.assertEqual(exact["summary"]["nginx_matches"], 1)
        self.assertTrue(exact["rows"][0]["origin"]["next"]["accepted"])

        for label, rows, clients in (
            (
                "missing",
                [{key: value for key, value in valid_origin.items() if key != "diagnostic_id"}],
                None,
            ),
            ("malformed", [{**valid_origin, "diagnostic_id": "not-a-diagnostic-id"}], None),
            (
                "unrelated",
                [{**valid_origin, "diagnostic_id": "tdiag-456-00002"}],
                None,
            ),
            ("duplicate-origin", [valid_origin, valid_origin], None),
            ("duplicate-client", [valid_origin], [client_row, client_row]),
            ("malformed-client", [valid_origin], [{**client_row, "diagnostic_id": "bad"}]),
            (
                "missing-client",
                [valid_origin],
                [{key: value for key, value in client_row.items() if key != "diagnostic_id"}],
            ),
        ):
            with self.subTest(label=label):
                report = joined(rows, clients)
                self.assertEqual(report["summary"]["nginx_matches"], 0)
                self.assertTrue(all(row["origin"] is None for row in report["rows"]))
                self.assertNotIn("diagnostic_id", str(report))


if __name__ == "__main__":
    unittest.main()
