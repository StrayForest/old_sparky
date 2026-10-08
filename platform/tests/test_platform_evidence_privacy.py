"""Adversarial checks for load, observer and live-QA evidence boundaries."""

from __future__ import annotations

from io import BytesIO, StringIO
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from tools.platform_evidence_sanitizer import (
    project_public_artifact,
    sanitized_log_summary,
)
from tools.platform_external_load import RequestResult, summarize_results
from tools.platform_external_load_observer import (
    _safe_postgres_wait_snapshot,
    _safe_process_lifecycle,
    postgres_statement_delta,
    summarize_ssr_observability,
)
from tools.platform_abort_retained_load import export_abort_evidence
from tools.platform_live_launch_report import sanitize_live_launch_lines
from tools.platform_production_qa import (
    _safe_qa_scenario_detail,
    cli_report_summary,
    response_diagnostics,
)


FORBIDDEN_VALUES = (
    "operator@example.test",
    "Authorization Bearer secret-token",
    "session=secret-session",
    "csrf=secret-csrf",
    "password=secret-password",
    "198.51.100.42",
    "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
    "INVITE-CODE",
    "SELECT email FROM users WHERE password='secret-password'",
    "/home/operator/private-report.json",
)


def serialized(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


class EvidencePrivacyTests(unittest.TestCase):
    def test_external_load_source_identity_is_closed_and_digest_bound(self) -> None:
        binding: dict[str, object] = {
            "schema": 1,
            "binding_mode": "verified-noop",
            "runner_sha": "a" * 40,
            "app_target_sha": "b" * 40,
            "baseline_identity": {
                "schema": 1,
                "source_sha": "b" * 40,
                "release_slug": "gha-123456-1-bbbbbbbbbbbb",
                "release_json_sha256": "c" * 64,
                "current_link_dev": 100,
                "current_link_ino": 101,
                "release_dev": 100,
                "release_ino": 102,
                "pending_operation": False,
            },
            "receipt_document_sha256": "d" * 64,
            "receipt_artifact_id": "456789",
            "receipt_artifact_name": "platform-production-noop-source-receipt-123456-2",
            "receipt_artifact_digest": "sha256:" + "e" * 64,
            "receipt_archive_sha256": "e" * 64,
            "cumulative_manifest_sha256": "f" * 64,
            "source_security_run_id": "234567",
            "source_security_run_attempt": "1",
            "autodeploy_run_id": "345678",
            "autodeploy_run_attempt": "3",
            "production_deploy_run_id": "123456",
            "production_deploy_run_attempt": "2",
        }
        canonical = json.dumps(
            binding, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        digest = hashlib.sha256(canonical).hexdigest()
        source: dict[str, object] = {
            "source_git_sha": "a" * 40,
            "app_target_sha": "b" * 40,
            "source_binding": binding,
            "source_binding_sha256": digest,
            "report_binding": {
                "checks": {
                    "source_git_sha": True,
                    "app_target_sha": True,
                    "source_binding": True,
                    "source_binding_sha256": True,
                }
            },
            "acceptance": {"passed": False, "decision": "SLO FAIL"},
        }

        projected = project_public_artifact("external_load", source)
        self.assertEqual(projected["source_git_sha"], "a" * 40)
        self.assertEqual(projected["app_target_sha"], "b" * 40)
        self.assertEqual(projected["source_binding"], binding)
        self.assertEqual(projected["source_binding_sha256"], digest)
        self.assertFalse(projected["acceptance"]["passed"])

        same_source = {
            **source,
            "app_target_sha": "a" * 40,
            "source_binding": None,
            "source_binding_sha256": None,
        }
        same_source["report_binding"] = {
            "checks": {
                "source_git_sha": True,
                "app_target_sha": True,
                "source_binding": True,
                "source_binding_sha256": True,
            }
        }
        projected_same_source = project_public_artifact("external_load", same_source)
        self.assertEqual(projected_same_source["source_git_sha"], "a" * 40)
        self.assertEqual(projected_same_source["app_target_sha"], "a" * 40)
        self.assertIsNone(projected_same_source["source_binding"])

        mutations: list[dict[str, object]] = []
        forged_app = copy.deepcopy(source)
        forged_app["app_target_sha"] = "a" * 40
        mutations.append(forged_app)
        bad_digest = copy.deepcopy(source)
        bad_digest["source_binding_sha256"] = "0" * 64
        mutations.append(bad_digest)
        for field, value in (
            ("unknown", "extension"),
            ("app_target_sha", "a" * 40),
            ("receipt_artifact_id", True),
        ):
            invalid_binding = copy.deepcopy(binding)
            invalid_binding[field] = value
            invalid_source = copy.deepcopy(source)
            invalid_source["source_binding"] = invalid_binding
            invalid_source["source_binding_sha256"] = hashlib.sha256(
                json.dumps(
                    invalid_binding,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
            ).hexdigest()
            mutations.append(invalid_source)
        false_check = copy.deepcopy(source)
        false_check["report_binding"]["checks"]["source_binding"] = False  # type: ignore[index]
        mutations.append(false_check)
        for index, invalid_source in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(ValueError):
                project_public_artifact("external_load", invalid_source)

        # The old report schema may contain only runner provenance. It remains
        # omitted instead of exposing an unverified identity value.
        legacy = project_public_artifact(
            "external_load",
            {"source_git_sha": "a" * 40, "report_binding": {"checks": {"run_identity": True}}},
        )
        self.assertNotIn("source_git_sha", legacy)

    def test_server_observability_projects_only_safe_request_perf_sampling_fields(
        self,
    ) -> None:
        report = project_public_artifact(
            "server_observability",
            {
                "server_request_perf_logs": {
                    "scope": "diagnostic_sample",
                    "logged_requests": 3,
                    "request_perf_sampling": {
                        "selection_reason_counts": {
                            "interval": 1,
                            "trigger_and_interval": 1,
                            "operator@example.test": 50,
                        },
                        "annotated_rows": 999,
                        "interval_selected_rows": 999,
                        "sample_interval": 16,
                        "client_identity": "Authorization Bearer secret-token",
                    },
                }
            },
        )

        request_perf = report["server_request_perf_logs"]
        self.assertEqual(request_perf["scope"], "diagnostic_sample")
        self.assertEqual(
            request_perf["request_perf_sampling"],
            {
                "selection_reason_counts": {
                    "interval": 1,
                    "trigger_and_interval": 1,
                },
                "annotated_rows": 2,
                "interval_selected_rows": 2,
                "sample_interval": 16,
            },
        )
        self.assertNotIn("client_identity", serialized(report))
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value.lower(), serialized(report).lower())

    def test_live_launch_report_drops_adversarial_values(self) -> None:
        lines = [
            (
                "LIVE_BROWSER_QA_FAILURE test=browser_public "
                "GET /api/v1/tournaments/private-slug/workspace?invite_code=INVITE-CODE "
                "status=500 Authorization Bearer secret-token "
                "Cookie=session=secret-session csrf=secret-csrf "
                "password=secret-password operator@example.test 198.51.100.42 "
                "https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
                "SELECT email FROM users WHERE password='secret-password' "
                "/home/operator/private-report.json"
            )
        ]
        report = sanitize_live_launch_lines(lines)
        output = serialized(report)
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["tests"][0]["route_class"], "tournament_workspace")
        self.assertEqual(report["tests"][0]["http_status"], 500)

        live_seen: list[int] = []

        def endless_live_lines():
            index = 0
            while True:
                live_seen.append(index)
                yield b"LIVE_BROWSER_QA_SUCCESS test=browser_public\n"
                index += 1

        bounded = sanitize_live_launch_lines(endless_live_lines(), max_lines=2)
        self.assertEqual(live_seen, [0, 1])
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["status"], "truncated")
        self.assertFalse(bounded["success"])

        giant = sanitize_live_launch_lines(
            BytesIO(
                b"LIVE_BROWSER_QA_FAILURE test=browser_public "
                + b"x" * 4096
                + b" operator@example.test Authorization Bearer secret-token"
            ),
            max_line_bytes=64,
        )
        self.assertTrue(giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(giant))
        self.assertNotIn("Authorization Bearer secret-token", serialized(giant))

        text_giant = sanitize_live_launch_lines(
            StringIO("ordinary output " + "x" * 4096 + " operator@example.test"),
            max_line_bytes=64,
        )
        self.assertTrue(text_giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(text_giant))

        invalid_utf8 = sanitize_live_launch_lines(BytesIO(b"\xff\xfe\xfa"))
        self.assertFalse(invalid_utf8["truncated"])
        self.assertEqual(invalid_utf8["line_count"], 1)

        def broken_live_stream():
            raise RuntimeError("operator@example.test Authorization Bearer secret-token")
            yield b"unreachable"

        broken = sanitize_live_launch_lines(broken_live_stream())
        self.assertEqual(broken["status"], "error")
        self.assertTrue(broken["truncated"])
        self.assertNotIn("operator@example.test", serialized(broken))

    def test_generic_canonical_log_sanitizer_drops_adversarial_values(self) -> None:
        summary = sanitized_log_summary(
            [
                "failed operator@example.test Authorization Bearer secret-token "
                "Cookie=session=secret-session 198.51.100.42 "
                "https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
                "SELECT email FROM users WHERE password='secret-password' "
                "/home/operator/private-report.json"
            ]
        )
        output = serialized(summary)
        for value in FORBIDDEN_VALUES:
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(set(summary), {
            "schema",
            "status",
            "line_count",
            "retained_line_count",
            "truncated",
            "class_counts",
            "route_class_counts",
            "status_counts",
        })

        seen: list[int] = []

        def endless_lines():
            index = 0
            while True:
                seen.append(index)
                yield b"ordinary output\n"
                index += 1

        bounded = sanitized_log_summary(endless_lines(), max_lines=3)
        self.assertEqual(seen, [0, 1, 2])
        self.assertTrue(bounded["truncated"])
        self.assertEqual(bounded["status"], "truncated")

        giant = sanitized_log_summary(
            BytesIO(
                b"ordinary output "
                + b"x" * 4096
                + b" operator@example.test password=secret-password"
            ),
            max_line_bytes=64,
        )
        self.assertTrue(giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(giant))
        self.assertNotIn("password=secret-password", serialized(giant))

        text_giant = sanitized_log_summary(
            StringIO("ordinary output " + "x" * 4096 + " operator@example.test"),
            max_line_bytes=64,
        )
        self.assertTrue(text_giant["truncated"])
        self.assertNotIn("operator@example.test", serialized(text_giant))

        invalid_utf8 = sanitized_log_summary(BytesIO(b"\xff\xfe\xfa"))
        self.assertFalse(invalid_utf8["truncated"])
        self.assertEqual(invalid_utf8["line_count"], 1)

        budget = sanitized_log_summary(
            BytesIO(b"safe prefix " + b"a" * 128 + b" secret-token"),
            max_total_bytes=32,
            max_line_bytes=64,
        )
        self.assertTrue(budget["truncated"])
        self.assertNotIn("secret-token", serialized(budget))

        def broken_log_stream():
            raise RuntimeError("operator@example.test password=secret-password")
            yield b"unreachable"

        broken = sanitized_log_summary(broken_log_stream())
        self.assertEqual(broken["status"], "error")
        self.assertTrue(broken["truncated"])
        self.assertNotIn("operator@example.test", serialized(broken))

    def test_external_http_summary_keeps_metrics_without_request_values(self) -> None:
        result = RequestResult(
            phase="ready-vote",
            method="POST",
            path=(
                "/tournaments/private-slug/deadlock/ready-check/vote"
                "?invite_code=INVITE-CODE"
            ),
            status=500,
            elapsed_ms=42.5,
            ok=False,
            response_bytes=12,
            time_to_first_byte_ms=10.5,
            cf_ray="unique-ray-id",
            cf_error_type="522",
            cf_error_origin="origin",
            error_kind="ValueError operator@example.test secret-token",
            response_json={"changed": False},
        )
        output = serialized(summarize_results([result]))
        for value in FORBIDDEN_VALUES + ("private-slug", "unique-ray-id"):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(json.loads(output)["by_route"], {
            "POST ready_vote": {
                "count": 1,
                "avg_ms": 42.5,
                "p50_ms": 42.5,
                "p90_ms": 42.5,
                "p95_ms": 42.5,
                "p99_ms": 42.5,
                "max_ms": 42.5,
            }
        })

    def test_response_diagnostics_is_fixed_schema(self) -> None:
        response = httpx.Response(
            500,
            headers={
                "Authorization": "Bearer secret-token",
                "Cookie": "session=secret-session",
                "CF-Error-Type": "522",
                "ETag": "operator@example.test",
            },
            content=(
                b"https://old-sparky.example/invite/INVITE-CODE?token=secret-token "
                b"/home/operator/private-report.json"
            ),
        )
        output = serialized(
            response_diagnostics(
                response,
                method="GET",
                path="/tournaments/private-slug?invite_code=INVITE-CODE",
            )
        )
        for value in FORBIDDEN_VALUES + ("private-slug",):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(
            json.loads(output)["route_class"],
            "tournament_detail",
        )

    def test_postgres_statement_delta_drops_raw_query_literals(self) -> None:
        raw_query = "SELECT email FROM users WHERE password='secret-password'"
        before = {
            "available": True,
            "rows": [{
                "queryid": "42",
                "query": raw_query,
                "calls": 1,
                "total_exec_ms": 1.0,
                "mean_exec_ms": 1.0,
                "rows": 1,
                "shared_blks_hit": 2,
                "shared_blks_read": 0,
                "temp_blks_written": 0,
            }],
        }
        after = {
            "available": True,
            "rows": [{**before["rows"][0], "calls": 2, "total_exec_ms": 3.5}],
        }
        output = serialized(postgres_statement_delta(before, after))
        self.assertNotIn(raw_query.lower(), output.lower())
        self.assertNotIn("query\":", output.lower())
        self.assertIn("\"queryid\": \"42\"", output)

    def test_observer_source_has_no_raw_postgres_query_projection(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "tools/platform_external_load_observer.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("activity.query", source)
        self.assertNotIn("pg_stat_statements.query", source)
        self.assertNotIn("'active_query_samples'", source)

    def test_observer_timeline_drops_backend_names_and_process_ids(self) -> None:
        waits = _safe_postgres_wait_snapshot(
            {
                "backend_ownership": [
                    {"application_name": "oldsparky-worker", "current": 2},
                    {"application_name": "operator@example.test", "current": 1},
                ],
                "wait_state_counts": {"Lock": 3, "secret-wait": 4},
                "backend_connections": 3,
            }
        )
        lifecycle = _safe_process_lifecycle(
            {
                "deadlock-api": {
                    "new_processes": [1234],
                    "missing_processes": [5678],
                    "new_process_starts": [{"pid": 1234, "started_at": "/home/operator"}],
                }
            }
        )
        output = serialized({"waits": waits, "lifecycle": lifecycle})
        for value in ("operator@example.test", "oldsparky-worker", "1234", "5678", "/home/operator"):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(waits["backend_ownership"], {"other": 1, "worker": 2})
        self.assertEqual(waits["wait_state_counts"], {"lock": 3, "other": 4})
        self.assertEqual(lifecycle["deadlock-api"], {"new_process_count": 1, "missing_process_count": 1})

        ssr_output = serialized(
            summarize_ssr_observability(
                [],
                [
                    {
                        "method": "GET",
                        "status": 504,
                        "uri": "/tournaments/private-slug?invite=INVITE-CODE",
                        "timeout_diagnostic_id": "tdiag-secret",
                        "request_id": "request-secret",
                        "cf_ray": "ray-secret",
                        "request_completion": "timeout",
                        "upstream_addr": "127.0.0.1:3000",
                        "upstream_status": "504",
                        "request_time": "30.0",
                    }
                ],
                [],
                timeout_diagnostic_ids={"tdiag-secret"},
            )
        )
        for value in FORBIDDEN_VALUES + (
            "private-slug",
            "INVITE-CODE",
            "tdiag-secret",
            "request-secret",
            "ray-secret",
        ):
            self.assertNotIn(value.lower(), ssr_output.lower())
        self.assertIn('"route_class": "tournament_page"', ssr_output)

    def test_cli_summary_exposes_only_control_preservation_boolean(self) -> None:
        report = {
            "marker": "preprod26082900000000ab",
            "mode": "scale",
            "passed": False,
            "control_account_preserved": True,
            "fatal_error": "RuntimeError operator@example.test password=secret-password",
            "report_path": "/home/operator/private-report.json",
            "tournament_slugs": ["private-slug"],
            "scenarios": [{
                "name": "workspace",
                "ok": False,
                "detail": {
                    "path": "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
                    "route_class": "tournament_workspace",
                    "status": 500,
                    "error_class": "Authorization Bearer secret-token",
                },
            }],
        }
        output = serialized(cli_report_summary(report))
        for value in FORBIDDEN_VALUES + ("private-slug",):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(json.loads(output)["control_account_preserved"], True)

        external_source = {
            "fixture_marker": "preprod26082900000000ab",
            "source_git_sha": "a" * 40,
            "external_run_id": "123456",
            "origin": "https://old-sparky.example/private?token=secret-token",
            "profile_id": "ready-vote-slo-v2",
            "profile_version": 2,
            "profile_digest": "b" * 64,
            "mode": "ready-vote",
            "users": 2,
            "tournaments": 1,
            "concurrency_stages": [16, 32],
            "dropped_work": 0,
            "passed": True,
            "status": "complete",
            "raw_http": {
                "requests": 10,
                "errors": 1,
                "latency": {"p95_ms": 42.5},
                "by_route": {
                    "POST ready_vote": {"p95_ms": 42.5},
                    "POST /tournaments/private-slug": {"p95_ms": 999.0},
                },
                "unknown_nested": {
                    "email": "operator@example.test",
                    "values": ["/home/operator/private-report.json"],
                },
                "error_samples": [{
                    "path": "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
                    "message": "password=secret-password",
                    "status": 500,
                }],
            },
            "logical": {
                "actions": 2,
                "final_successes": 2,
                "final_failures": 0,
                "end_to_end_latency": {"p95_ms": 44.5},
            },
            "acceptance": {
                "passed": True,
                "contract_ok": True,
                "decision": "SLO PASS",
                "checks": {"complete": True, "run_identity": True},
            },
            "phases": {
                "capacity_ramp": {
                    "concurrency_stages": [16, 32],
                    "stages": {"16": {"raw_http": {"requests": 2}}},
                    "analysis": {
                        "recommended_max_concurrency": 16,
                        "rollout_required": True,
                    },
                },
            },
            "report_binding": {
                "checks": {"run_identity": True},
                "actual": {"source_git_sha": "a" * 40, "external_run_id": "123456"},
            },
            "unknown": {
                "email": "operator@example.test",
                "auth": "Authorization Bearer secret-token",
                "query": "SELECT email FROM users WHERE password='secret-password'",
                "list": ["invite=INVITE-CODE", "/home/operator/private-report.json"],
            },
        }
        public_report = project_public_artifact("external_load", external_source)
        public_output = serialized(public_report)
        for value in FORBIDDEN_VALUES + (
            "preprod26082900000000ab",
            "a" * 40,
            "123456",
        ):
            self.assertNotIn(value.lower(), public_output.lower())
        self.assertEqual(public_report["raw_http"]["requests"], 10)
        self.assertEqual(public_report["raw_http"]["latency"]["p95_ms"], 42.5)
        self.assertTrue(public_report["acceptance"]["checks"]["complete"])
        self.assertEqual(public_report["concurrency_stages"], [16, 32])
        self.assertEqual(public_report["phases"]["capacity_ramp"]["concurrency_stages"], [16, 32])
        self.assertEqual(
            public_report["phases"]["capacity_ramp"]["analysis"]["recommended_max_concurrency"],
            16,
        )
        self.assertTrue(public_report["report_binding"]["checks"]["run_identity"])
        self.assertNotIn("actual", public_report["report_binding"])
        self.assertNotIn("unknown", public_report)

        for completed_decision in (
            "STRESS BEHAVIOR FAIL",
            "SPIKE BEHAVIOR FAIL",
            "CAPACITY EXPERIMENT COMPLETE TARGET FAIL",
        ):
            projected = project_public_artifact(
                "external_load",
                {
                    "acceptance": {
                        "passed": False,
                        "decision": completed_decision,
                        "pending_origin_evidence": False,
                        "checks": {"contract": True},
                    }
                },
            )
            self.assertEqual(projected["acceptance"]["decision"], completed_decision)
        unknown_decision = project_public_artifact(
            "external_load",
            {"acceptance": {"passed": False, "decision": "operator text secret"}},
        )
        self.assertEqual(unknown_decision["acceptance"]["decision"], "other")

        invalid_types = project_public_artifact(
            "external_load",
            {
                "users": "2",
                "raw_http": {"requests": "10", "latency": ["42.5"]},
                "acceptance": {"passed": "true", "checks": ["complete"]},
                "unknown_string": "operator@example.test",
            },
        )
        self.assertNotIn("users", invalid_types)
        self.assertNotIn("raw_http", invalid_types)
        self.assertNotIn("passed", invalid_types["acceptance"])

        observer_source = {
            "schema": 1,
            "binding": {
                "fixture_marker": "preprod26082900000000ab",
                "external_run_id": "123456",
                "complete": True,
            },
            "started_at": "2026-09-12T00:00:00+00:00",
            "system": {
                "samples": 2,
                "memory": {"p95": 123.0},
                "processes": {
                    "deadlock-api": {
                        "process_count": 1,
                        "rss_bytes": 123456,
                        "pid": 987654,
                        "uid": 876543,
                        "home_path": "/home/operator/private-report.json",
                    },
                },
                "timeline": [{"pid": 987654, "ppid": 123, "start_time_ticks": 44}],
                "cpu_per_core": {"cpu0": {"p95": 20.0}},
            },
            "server_ssr_observability": {
                "event_loop": {
                    "samples": 2,
                    "samples_detail": [{"pid": 987654, "p95_ms": 8.0}],
                    "by_pid": {"987654": {"memory": {"rss_bytes": 123456}}},
                },
                "nginx_api": {
                    "requests": 1,
                    "by_method_route": {
                        "GET tournament_detail": {"p95_ms": 8.0},
                        "GET /tournaments/private-slug": {"p95_ms": 99.0},
                    },
                },
            },
            "cpu_profile": {
                "enabled": True,
                "armed_worker_identities": [{"pid": 987654, "uid": 876543}],
                "profiles": [
                    {
                        "profile_available": True,
                        "filename": "/home/operator/private-profile.pstats",
                        "pid": 987654,
                        "functions": [
                            {
                                "function": "ReadyVoteService.cast_vote",
                                "line": 42,
                                "primitive_calls": 8,
                                "calls": 12,
                                "self_seconds": 0.125,
                                "cumulative_seconds": 0.75,
                                "filename": "/home/operator/private-source.py",
                                "pid": 987654,
                            },
                            {
                                "function": "<dynamic function at 0x1234>",
                                "line": 3.0,
                                "primitive_calls": "1",
                                "calls": 1_000_000_000_001,
                                "self_seconds": float("nan"),
                                "cumulative_seconds": -1.0,
                                "filename": "/home/operator/private-source.py",
                            },
                            {"function": "192.168.1.1"},
                            {"function": "operator@example.test"},
                            {"function": "/tmp/private.py"},
                            {"function": "987654"},
                            {
                                "function": "<lambda>",
                                "self_seconds": float("inf"),
                                "cumulative_seconds": 0.0,
                            },
                        ],
                    }
                ],
                "retention": {
                    "available_profiles": 1,
                    "ignored_stale_profiles": 2,
                    "invalid_profiles": 1,
                },
                "signal_delivery": {
                    "arm": {
                        "requested_count": 4,
                        "delivered_count": 1,
                        "rejected_count": 3,
                        "pidfd_api_available": False,
                        "rejection_reasons": {
                            "pidfd_unavailable": 1,
                            "uid_unavailable": 1,
                            "worker_missing": 1,
                            "operator@example.test": 99,
                        },
                    },
                    "flush": {
                        "requested_count": 0,
                        "delivered_count": 0,
                        "rejected_count": 0,
                        "pidfd_api_available": False,
                        "availability_reason": "uid_unavailable",
                    },
                },
            },
            "postgres_stat_statements": {
                "before": {
                    "available": True,
                    "rows": [{
                        "queryid": "42",
                        "query": "SELECT email FROM users WHERE password='secret-password'",
                        "calls": 1,
                    }],
                },
            },
            "unknown": {
                "email": "operator@example.test",
                "authorization": "Bearer secret-token",
                "url": "https://old-sparky.example/invite/INVITE-CODE?token=secret-token",
                "values": ["/home/operator/private-report.json"],
            },
        }
        public_observer = project_public_artifact("server_observability", observer_source)
        observer_output = serialized(public_observer)
        for value in FORBIDDEN_VALUES + (
            "987654",
            "876543",
        ):
            self.assertNotIn(value.lower(), observer_output.lower())
        self.assertEqual(public_observer["system"]["processes"]["deadlock-api"]["rss_bytes"], 123456)
        self.assertNotIn("binding", public_observer)
        self.assertNotIn("by_pid", public_observer["server_ssr_observability"]["event_loop"])
        self.assertNotIn("armed_worker_identities", public_observer["cpu_profile"])
        self.assertEqual(public_observer["cpu_profile"]["ignored_stale_profiles"], 2)
        profile = public_observer["cpu_profile"]["profiles"][0]
        self.assertEqual(profile["profile_available"], True)
        self.assertEqual(
            profile["functions"][0],
            {
                "function": "ReadyVoteService.cast_vote",
                "line": 42,
                "primitive_calls": 8,
                "calls": 12,
                "self_seconds": 0.125,
                "cumulative_seconds": 0.75,
            },
        )
        self.assertEqual(profile["functions"][1]["function"], "other")
        self.assertNotIn("line", profile["functions"][1])
        self.assertNotIn("primitive_calls", profile["functions"][1])
        self.assertNotIn("calls", profile["functions"][1])
        self.assertNotIn("self_seconds", profile["functions"][1])
        self.assertNotIn("cumulative_seconds", profile["functions"][1])
        self.assertEqual(
            [row["function"] for row in profile["functions"][1:]],
            ["other", "other", "other", "other", "<lambda>"],
        )
        self.assertNotIn("self_seconds", profile["functions"][5])
        self.assertEqual(profile["functions"][5]["cumulative_seconds"], 0.0)
        self.assertNotIn("filename", profile)
        self.assertNotIn("pid", profile)
        self.assertNotIn("private-profile", observer_output)
        self.assertNotIn("private-source", observer_output)
        self.assertNotIn("dynamic function", observer_output)
        self.assertNotIn("192.168.1.1", observer_output)
        self.assertNotIn("operator@example.test", observer_output)
        self.assertNotIn("/tmp/private.py", observer_output)
        self.assertNotIn("987654", observer_output)
        self.assertEqual(
            public_observer["cpu_profile"]["signal_delivery"]["arm"]["rejection_reasons"],
            {
                "pidfd_unavailable": 1,
                "uid_unavailable": 1,
                "worker_missing": 1,
            },
        )
        self.assertEqual(
            public_observer["cpu_profile"]["signal_delivery"]["flush"]["availability_reason"],
            "uid_unavailable",
        )
        self.assertEqual(public_observer["postgres_stat_statements"]["before"]["rows"][0]["queryid"], "42")

        bounded_profiles = project_public_artifact(
            "server_observability",
            {
                "cpu_profile": {
                    "profiles": [
                        {
                            "profile_available": True,
                            "functions": [
                                {
                                    "function": "f",
                                    "line": 1,
                                    "primitive_calls": 1,
                                    "calls": 1,
                                    "self_seconds": 0.1,
                                    "cumulative_seconds": 0.2,
                                }
                            ]
                            * 101,
                        }
                    ]
                    * 33
                }
            },
        )["cpu_profile"]["profiles"]
        self.assertEqual(len(bounded_profiles), 32)
        self.assertEqual(len(bounded_profiles[0]["functions"]), 100)

        timeout_source = {
            "schema": 1,
            "profile_id": "authenticated-page-load-v1",
            "summary": {
                "client_timeout_errors": 1,
                "classifications": {"before_origin_observed": 1, "dynamic-route": 9},
                "error": "Authorization Bearer secret-token",
            },
            "rows": [{
                "observation_index": 0,
                "classification": "before_origin_observed",
                "classification_reason_class": "no_origin_observation",
                "origin": {
                    "route_class": "tournament_detail",
                    "method": "GET",
                    "status": 504,
                    "path": "/tournaments/private-slug?invite=INVITE-CODE",
                    "request_id": "request-secret",
                },
                "error_path": "/home/operator/private-report.json",
                "unknown": ["SELECT email FROM users WHERE password='secret-password'"],
            }],
            "dynamic_route_map": {"GET /tournaments/private-slug": 1},
        }
        public_timeout = project_public_artifact("timeout_diagnostics", timeout_source)
        timeout_output = serialized(public_timeout)
        for value in FORBIDDEN_VALUES + ("private-slug", "request-secret"):
            self.assertNotIn(value.lower(), timeout_output.lower())
        self.assertEqual(public_timeout["summary"]["client_timeout_errors"], 1)
        self.assertEqual(public_timeout["rows"][0]["origin"]["route_class"], "tournament_detail")
        self.assertNotIn("dynamic_route_map", public_timeout)

        # The step summary consumes only this public projection. Keep its
        # source contract free of private binding/runner values as well.
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/platform-production-external-load.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("payload.get('source_git_sha')", workflow)
        self.assertNotIn("payload.get('load_contract')", workflow)
        for artifact_type in (
            "external_load",
            "server_observability",
            "timeout_diagnostics",
        ):
            self.assertIn(f"--artifact-type {artifact_type}", workflow)

    def test_durable_scenario_detail_drops_adversarial_values(self) -> None:
        detail = _safe_qa_scenario_detail(
            {
                "email": "operator@example.test",
                "authorization": "Bearer secret-token",
                "cookie": "session=secret-session; csrf=secret-csrf",
                "password": "secret-password",
                "path": "https://old-sparky.example/api/v1/tournaments/private-slug/invite?token=secret-token",
                "request_id": "req-secret",
                "sql": "SELECT email FROM users WHERE password='secret-password'",
                "remote_ip": "198.51.100.42",
                "safe_status": 500,
                "nested": ["/home/operator/private-report.json", "INVITE-CODE"],
            }
        )
        output = serialized(detail)
        for value in FORBIDDEN_VALUES + ("req-secret",):
            self.assertNotIn(value.lower(), output.lower())
        self.assertEqual(detail["email"], True)
        self.assertEqual(detail["route_class"], "tournament_participants")
        self.assertEqual(detail["request_id_present"], True)
        self.assertEqual(detail["nested"], {"count": 2})

    def test_retained_abort_exports_projected_observer_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runs" / "gha-123"
            export_root = root / "exports"
            run_root.mkdir(parents=True)
            export_root.mkdir()
            (run_root / "server-observability.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "binding": {
                            "fixture_marker": "preprod26082900000000ab",
                            "external_run_id": "123",
                            "complete": True,
                        },
                        "system": {
                            "processes": {
                                "deadlock-api": {
                                    "process_count": 1,
                                    "pid": 321,
                                    "uid": 654,
                                    "ppid": 987,
                                    "start_time_ticks": 111,
                                    "rss_bytes": 123456,
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("tools.platform_abort_retained_load.RUN_ROOT_BASE", root / "runs"), \
                mock.patch("tools.platform_abort_retained_load.ABORT_EXPORT_BASE", export_root), \
                mock.patch.dict(
                    os.environ,
                    {
                        "SUDO_UID": str(os.geteuid()),
                        "SUDO_GID": str(os.getegid()),
                    },
                ):
                exported = export_abort_evidence("123", '{"schema":1,"process_count":1}\n')
            payload = json.loads((exported / "server-observability.json").read_text(encoding="utf-8"))
            output = serialized(payload)
            self.assertNotIn("preprod26082900000000ab", output)
            self.assertNotIn("321", output)
            self.assertNotIn("654", output)
            self.assertNotIn("987", output)
            self.assertNotIn("111", output)
            self.assertEqual(payload["public_projection"], True)
            self.assertEqual(payload["authoritative"], False)
            self.assertEqual(
                payload["system"]["processes"]["deadlock-api"]["rss_bytes"],
                123456.0,
            )


if __name__ == "__main__":
    unittest.main()
