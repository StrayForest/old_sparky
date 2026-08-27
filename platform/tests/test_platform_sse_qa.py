from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx

from tools.platform_cleanup_retained_matrix import load_matrix_manifest
from tools.platform_sse_qa import (
    SSE_EVENT_TYPE,
    SSE_HOLD_MAX_SECONDS,
    NormalApiMetrics,
    SseMetrics,
    _close_sse_stream_context,
    combined_profile_timeout_seconds,
    max_sse_open_timeout_seconds,
    plateau_probe_count,
    sse_open_delay_seconds,
    summary,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class PlatformSseQaTests(unittest.TestCase):
    def test_normal_api_metrics_keep_kind_and_latency_accounting(self) -> None:
        metrics = NormalApiMetrics()
        metrics.record_success(status_code=200, elapsed_ms=12.0, kind="workspace")
        metrics.record_success(status_code=304, elapsed_ms=18.0, kind="workspace")
        metrics.record_error(
            RuntimeError("temporary API error"),
            path="/auth/session",
            elapsed_ms=25.0,
            kind="auth_session",
        )

        result = metrics.summary()

        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["successes"], 2)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["statuses"], {"200": 1, "304": 1})
        self.assertEqual(result["kinds"]["workspace"], 2)
        self.assertEqual(result["kinds"]["auth_session:error"], 1)
        self.assertEqual(result["error_samples"][0]["path"], "/auth/session")

    def test_metrics_record_admission_fanout_and_latency_percentiles(self) -> None:
        metrics = SseMetrics()
        for _ in range(4):
            metrics.mark("connection_attempts")
        for _ in range(2):
            metrics.connection_opened()
        metrics.connection_closed()
        metrics.mark("rejected_429")
        metrics.mark("rejected_429")
        metrics.response_status(429)
        metrics.response_status(429)
        metrics.mark("events", 3)
        metrics.mark("resyncs", 2)
        metrics.connect_latencies.extend([1.0, 2.0])
        metrics.event_latencies.extend([3.0, 5.0, 7.0])

        result = metrics.summary()

        self.assertEqual(result["connection_attempts"], 4)
        self.assertEqual(result["connected"], 2)
        self.assertEqual(result["max_active_connections"], 2)
        self.assertEqual(result["active_connections"], 1)
        self.assertEqual(result["rejected_429"], 2)
        self.assertEqual(result["response_statuses"], {"429": 2})
        self.assertEqual(result["response_error_samples"], [])
        self.assertEqual(result["events"], 3)
        self.assertEqual(result["resyncs"], 2)
        self.assertEqual(result["connected_percent"], 50.0)
        self.assertEqual(result["connect_latency_ms"]["p95"], 1.95)
        self.assertAlmostEqual(result["event_delivery_latency_ms"]["p99"], 6.96)

    def test_metrics_keep_bounded_non_200_response_diagnostics(self) -> None:
        metrics = SseMetrics()
        headers = httpx.Headers(
            {
                "content-type": "application/json",
                "server": "nginx",
                "cf-ray": "abc123-SIN",
                "cf-cache-status": "DYNAMIC",
            }
        )
        for _ in range(30):
            metrics.record_response_error(
                status_code=500,
                body=b'{"detail":"pool timeout"}',
                headers=headers,
            )

        samples = metrics.summary()["response_error_samples"]
        self.assertEqual(len(samples), 25)
        self.assertEqual(samples[0]["status"], 500)
        self.assertEqual(samples[0]["body"], '{"detail":"pool timeout"}')
        self.assertEqual(samples[0]["cf_ray"], "abc123-SIN")
        self.assertEqual(samples[0]["cf_cache_status"], "DYNAMIC")

    def test_open_timeout_is_reported_as_polling_fallback_signal(self) -> None:
        metrics = SseMetrics()
        metrics.mark("open_timeouts")
        metrics.mark("fallback_polling_eligible")

        result = metrics.summary()

        self.assertEqual(result["open_timeouts"], 1)
        self.assertEqual(result["fallback_polling_eligible"], 1)
        self.assertEqual(result["errors"], 0)

    def test_timeout_ceiling_is_diagnostic_only(self) -> None:
        self.assertEqual(max_sse_open_timeout_seconds("http://127.0.0.1:8010"), 60.0)
        self.assertEqual(max_sse_open_timeout_seconds("http://localhost:8010"), 60.0)
        self.assertEqual(max_sse_open_timeout_seconds("https://old-sparky.com"), 60.0)

    def test_ready_check_open_delay_honors_signed_admission_slot(self) -> None:
        self.assertEqual(
            sse_open_delay_seconds(
                index=0,
                open_rate_per_second=25,
                scheduled_open_at=130.0,
                now_epoch=100.0,
            ),
            30.0,
        )
        self.assertEqual(
            sse_open_delay_seconds(
                index=100,
                open_rate_per_second=25,
                scheduled_open_at=101.0,
                now_epoch=100.0,
            ),
            4.0,
        )

    def test_qa_hold_can_prove_stream_lifetime_beyond_old_rotation_boundary(self) -> None:
        self.assertGreater(SSE_HOLD_MAX_SECONDS, 600.0)

    def test_explicit_capacity_plateau_always_gets_n_plus_ten_probe(self) -> None:
        self.assertEqual(
            plateau_probe_count(capacity_limit=3_000, connection_count=3_000),
            10,
        )
        self.assertEqual(
            plateau_probe_count(capacity_limit=15_000, connection_count=15_000),
            10,
        )
        self.assertEqual(
            plateau_probe_count(capacity_limit=3_000, connection_count=2_999),
            0,
        )
        self.assertEqual(
            plateau_probe_count(capacity_limit=0, connection_count=3_000),
            0,
        )


class PlatformSseQaAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_http_context_close_is_bounded(self) -> None:
        class SlowContext:
            async def __aexit__(self, *_args) -> None:
                await asyncio.sleep(10)

        started = asyncio.get_running_loop().time()
        await _close_sse_stream_context(SlowContext())

        self.assertLess(asyncio.get_running_loop().time() - started, 1)

    def test_combined_profile_has_a_bounded_execution_budget(self) -> None:
        self.assertEqual(
            combined_profile_timeout_seconds(
                polling_duration_seconds=30,
                polling_open_stagger_seconds=60,
                http_timeout_seconds=10,
            ),
            115,
        )
        self.assertEqual(
            combined_profile_timeout_seconds(
                polling_duration_seconds=1,
                polling_open_stagger_seconds=0,
                http_timeout_seconds=1,
            ),
            30,
        )
        self.assertEqual(
            combined_profile_timeout_seconds(
                polling_duration_seconds=30,
                polling_open_stagger_seconds=0,
                http_timeout_seconds=10,
                sse_duration_seconds=605,
                sse_open_span_seconds=120,
            ),
            740,
        )

    def test_error_samples_keep_bounded_stream_correlation(self) -> None:
        metrics = SseMetrics()
        metrics.record_error(
            httpx.RemoteProtocolError("incomplete chunked read"),
            details={
                "request_id": "sseqa-test",
                "elapsed_ms": 60012.3,
                "bytes_received": 128,
                "events": 3,
                "keepalives": 4,
                "cf_ray": "ray-test",
            },
        )

        sample = metrics.summary()["error_samples"][0]
        self.assertEqual(sample["request_id"], "sseqa-test")
        self.assertEqual(sample["elapsed_ms"], 60012.3)
        self.assertEqual(sample["cf_ray"], "ray-test")

    def test_summary_is_an_exact_cleanup_manifest(self) -> None:
        marker = "preprod260825120000abcd"
        user_id = str(uuid4())
        tournament_id = str(uuid4())
        report = {
            "marker": marker,
            "mode": "combined",
            "origin": "https://old-sparky.com",
            "control_email": "aleksei.lisitsin1@gmail.com",
            "report_path": "/tmp/run/combined/combined.json",
            "user_ids": [user_id],
            "tournament_ids": [tournament_id],
            "requested_users": 1,
            "sse": {
                "target_connections": 1,
                "load_generator_resources": {
                    "nofile_soft": 32768,
                    "nofile_hard": 32768,
                },
                "metrics": {
                    "connected": 1,
                    "error_samples": [],
                    "response_error_samples": [],
                },
            },
            "polling": {"tabs_planned": 1, "executed": 1},
            "performance": {},
            "passed": True,
        }

        compact = summary(report)

        self.assertEqual(compact["mode"], "combined")
        self.assertEqual(compact["control_email"], "aleksei.lisitsin1@gmail.com")
        self.assertEqual(compact["completed_tournaments"], 1)
        self.assertEqual(compact["rows"][0]["synthetic_users"], 1)
        self.assertEqual(compact["rows"][0]["result"]["marker"], marker)
        self.assertEqual(compact["sse"]["load_generator_resources"]["nofile_soft"], 32768)
        self.assertEqual(compact["sse"]["error_samples"], [])

    def test_sse_manifest_is_accepted_by_exact_cleanup_validator(self) -> None:
        marker = "preprod260825120000abcd"
        user_id = str(uuid4())
        tournament_id = str(uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "sse" / "sse.json"
            report_path.parent.mkdir()
            report_path.write_text(
                json.dumps(
                    {
                        "marker": marker,
                        "report_path": str(report_path),
                        "mode": "sse",
                        "origin": "https://old-sparky.com",
                        "user_ids": [user_id],
                        "tournament_ids": [tournament_id],
                        "tournament_visibility": "public",
                    }
                ),
                encoding="utf-8",
            )
            summary_path = root / "sse" / "matrix-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "mode": "sse",
                        "control_email": "aleksei.lisitsin1@gmail.com",
                        "completed_tournaments": 1,
                        "rows": [
                            {
                                "synthetic_users": 1,
                                "report_path": str(report_path),
                                "result": {
                                    "marker": marker,
                                    "report_path": str(report_path),
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path.chmod(0o600)
            summary_path.chmod(0o600)

            manifest = load_matrix_manifest(
                summary_path,
                run_root=root,
                expected_control_email="aleksei.lisitsin1@gmail.com",
            )

        self.assertEqual(manifest["mode"], "sse")
        self.assertEqual(manifest["user_ids"], {user_id})
        self.assertEqual(manifest["tournament_ids"], {tournament_id})

    def test_sse_contract_is_present_in_production_contour(self) -> None:
        supervisor = (
            REPO_ROOT / "platform/tools/platform_production_retained_load_matrix_qa.sh"
        ).read_text()
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-retained-load-matrix.yml"
        ).read_text()
        cleanup = (
            REPO_ROOT / "platform/tools/platform_production_retained_load_cleanup_qa.sh"
        ).read_text()
        abort = (REPO_ROOT / "platform/tools/platform_abort_retained_load.py").read_text()

        self.assertEqual(SSE_EVENT_TYPE, "qa_sse_probe")
        for profile in ("sse", "combined"):
            self.assertIn(profile, supervisor)
            self.assertIn(profile, workflow)
        self.assertIn("platform_sse_qa.py", supervisor)
        self.assertIn('sse_setup_concurrency=20', supervisor)
        self.assertIn('sse_setup_concurrency=4', supervisor)
        self.assertIn('if [[ "$sse_scope" == "ready-check" ]]', supervisor)
        self.assertIn('--concurrency "$sse_setup_concurrency"', supervisor)
        self.assertIn('--http-timeout 10', supervisor)
        self.assertIn('--request-origin "$EXPECTED_ORIGIN"', supervisor)
        self.assertIn('--sse-open-timeout "$sse_open_timeout"', supervisor)
        self.assertIn('--sse-open-rate "$sse_open_rate"', supervisor)
        self.assertIn('--sse-capacity-limit "$sse_capacity_limit"', supervisor)
        self.assertIn('--sse-admission-mode "$sse_admission_mode"', supervisor)
        self.assertIn('--sse-scope "$sse_scope"', supervisor)
        self.assertIn('sse_scope=ready-check', supervisor)
        self.assertIn('sse_scope', workflow)
        self.assertIn("sse_duration <= 900", supervisor)
        self.assertIn('sse_origin="http://127.0.0.1:8010"', supervisor)
        self.assertIn('--control-email "$control_email"', supervisor)
        self.assertIn('ulimit -n "$nofile_target"', supervisor)
        self.assertIn('SSE load-generator nofile', supervisor)
        sse_source = (REPO_ROOT / "platform/tools/platform_sse_qa.py").read_text()
        self.assertIn('"hot-public-single-tournament"', sse_source)
        self.assertIn('SSE_FIXTURE_TIMEOUT_SECONDS = 90.0', sse_source)
        self.assertIn('mode="sse"', sse_source)
        self.assertIn('default="ticket"', sse_source)
        self.assertIn('issue_public_sse_admission_ticket', sse_source)
        self.assertIn('prepare_ready_check_fixture', sse_source)
        self.assertIn('"/ready-check/events"', sse_source)
        self.assertIn('"/ready-check/state"', sse_source)
        self.assertIn('probe_ready_check_overflow', sse_source)
        self.assertIn('"ready-check-ticket"', sse_source)
        self.assertIn('scale_users=requested_users', sse_source)
        self.assertIn('"max_subscribers_reported"', sse_source)
        self.assertIn('"publisher": sse.get("publisher")', sse_source)
        self.assertIn("all_attempts_done", sse_source)
        self.assertIn("sse_event_delivery_complete", sse_source)
        self.assertIn("expected_events", sse_source)
        self.assertIn('metrics["initial_connected"] == args.sse_connections', sse_source)
        self.assertIn('"initial_connected": metrics.get("initial_connected")', sse_source)
        self.assertIn("SSE_RECONNECT_MAX_BACKOFF_SECONDS", sse_source)
        self.assertIn("secrets.randbelow(1_000_000)", sse_source)
        self.assertIn("fallback_polling_eligible", sse_source)
        self.assertIn('"resyncs": metrics.get("resyncs")', sse_source)
        self.assertIn("plateau_probe", sse_source)
        self.assertIn("SSE_QA_GLOBAL_LIMIT_MAX", sse_source)
        self.assertIn("fatal_traceback", sse_source)
        self.assertIn("SSE_HOLD_MAX_SECONDS = 900.0", sse_source)
        self.assertIn("performance_collection_error", sse_source)
        self.assertIn("--request-origin", sse_source)
        self.assertIn('"scenarios": report.get("scenarios", [])', sse_source)
        self.assertIn('RLIMIT_NOFILE', sse_source)
        self.assertIn('"combined_polling_requests_without_errors",', sse_source)
        self.assertIn('"combined_sse_no_unexpected_errors",', sse_source)
        self.assertIn("run_normal_api_traffic", sse_source)
        self.assertIn('not qa.report.get("fatal_error")', sse_source)
        self.assertIn('"combined_normal_api_without_errors",', sse_source)
        self.assertIn('"invite_create"', sse_source)
        self.assertIn('"auth_session"', sse_source)
        self.assertIn("args.ready_file", sse_source)
        self.assertIn("polling_request_gate = asyncio.Semaphore", sse_source)
        self.assertIn('"request_concurrency"', sse_source)
        self.assertIn("return_when=asyncio.ALL_COMPLETED", sse_source)
        self.assertIn('and sse_report["metrics"]["rejected_503"] == 0', sse_source)
        self.assertIn('fatal=True,', sse_source)
        self.assertIn("platform_recover_retained_browser_report.py", cleanup)
        self.assertIn('--mode "$recovery_profile"', cleanup)
        self.assertIn('chmod 0700 -- "$run_root"', cleanup)
        self.assertIn("if SCRIPT_PATH in args and run_id in args:", abort)
        self.assertIn("ABORT_EVIDENCE_EXPORT=", abort)
        self.assertIn("server-observability.log", abort)

    def test_failure_recovery_contour_is_allowlisted_and_exactly_cleaned(self) -> None:
        supervisor = (
            REPO_ROOT
            / "platform/tools/platform_production_sse_failure_recovery_qa.sh"
        ).read_text()
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-sse-failure-recovery.yml"
        ).read_text()
        publisher = (
            REPO_ROOT / "platform/tools/platform_sse_publish_probe.py"
        ).read_text()

        for fault in (
            "api-worker-restart",
            "api-restart",
            "redis-hiccup",
            "nginx-reload",
            "mass-disconnect",
        ):
            self.assertIn(fault, supervisor)
            self.assertIn(fault, workflow)
        self.assertIn("RUN-PRODUCTION-SSE-FAILURE-RECOVERY", supervisor)
        self.assertIn("flock -n 9", supervisor)
        self.assertIn("systemctl restart deadlock-api.service", supervisor)
        self.assertIn("systemctl restart deadlock-worker.service", supervisor)
        self.assertIn("systemctl restart redis-server.service", supervisor)
        self.assertIn("systemctl reload nginx.service", supervisor)
        self.assertIn("platform_sse_publish_probe.py", supervisor)
        self.assertIn('"recovery_subscribers"', supervisor)
        self.assertIn("PRODUCTION_SSE_FAILURE_RECOVERY_PASSED=1", supervisor)
        self.assertNotIn("systemctl stop", supervisor)
        self.assertNotIn("eval ", supervisor)
        self.assertIn("if: ${{ always() && needs.failure-recovery.result != 'skipped' }}", workflow)
        self.assertIn("platform_production_retained_load_cleanup_qa.sh", workflow)
        self.assertIn("DELETE-PRODUCTION-RETAINED-LOAD", workflow)
        self.assertIn("actions/upload-artifact@v6", workflow)
        self.assertIn("--tournament-id", publisher)
        self.assertIn("bracket_channel", publisher)
