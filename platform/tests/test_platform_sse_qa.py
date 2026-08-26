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
    SseMetrics,
    _close_sse_stream_context,
    combined_profile_timeout_seconds,
    max_sse_open_timeout_seconds,
    summary,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class PlatformSseQaTests(unittest.TestCase):
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
        self.assertIn('--concurrency "$sse_setup_concurrency"', supervisor)
        self.assertIn('--http-timeout 10', supervisor)
        self.assertIn('--request-origin "$EXPECTED_ORIGIN"', supervisor)
        self.assertIn('--sse-open-timeout "$sse_open_timeout"', supervisor)
        self.assertIn('--sse-admission-mode "$sse_admission_mode"', supervisor)
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
        self.assertIn('scale_users=requested_users', sse_source)
        self.assertIn('"max_subscribers_reported"', sse_source)
        self.assertIn('"publisher": sse.get("publisher")', sse_source)
        self.assertIn("all_attempts_done", sse_source)
        self.assertIn("sse_event_delivery_complete", sse_source)
        self.assertIn("expected_events", sse_source)
        self.assertIn("fallback_polling_eligible", sse_source)
        self.assertIn("fatal_traceback", sse_source)
        self.assertIn("performance_collection_error", sse_source)
        self.assertIn("--request-origin", sse_source)
        self.assertIn('"scenarios": report.get("scenarios", [])', sse_source)
        self.assertIn('RLIMIT_NOFILE', sse_source)
        self.assertIn('"combined_polling_requests_without_errors",', sse_source)
        self.assertIn('"combined_sse_no_unexpected_errors",', sse_source)
        self.assertIn("polling_request_gate = asyncio.Semaphore", sse_source)
        self.assertIn('"request_concurrency"', sse_source)
        self.assertIn("return_when=asyncio.ALL_COMPLETED", sse_source)
        self.assertIn('and sse_report["metrics"]["rejected_503"] == 0', sse_source)
        self.assertIn('fatal=True,', sse_source)
        self.assertIn("platform_recover_retained_browser_report.py", cleanup)
        self.assertIn('--mode "$recovery_profile"', cleanup)
        self.assertIn("if SCRIPT_PATH in args and run_id in args:", abort)
        self.assertIn("ABORT_EVIDENCE_EXPORT=", abort)
        self.assertIn("server-observability.log", abort)
