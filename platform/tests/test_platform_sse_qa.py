from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from tools.platform_cleanup_retained_matrix import load_matrix_manifest
from tools.platform_sse_qa import SSE_EVENT_TYPE, SseMetrics, summary


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
        metrics.mark("events", 3)
        metrics.connect_latencies.extend([1.0, 2.0])
        metrics.event_latencies.extend([3.0, 5.0, 7.0])

        result = metrics.summary()

        self.assertEqual(result["connection_attempts"], 4)
        self.assertEqual(result["connected"], 2)
        self.assertEqual(result["max_active_connections"], 2)
        self.assertEqual(result["active_connections"], 1)
        self.assertEqual(result["rejected_429"], 2)
        self.assertEqual(result["events"], 3)
        self.assertEqual(result["connected_percent"], 50.0)
        self.assertEqual(result["connect_latency_ms"]["p95"], 1.95)
        self.assertAlmostEqual(result["event_delivery_latency_ms"]["p99"], 6.96)

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
            "sse": {"target_connections": 1, "metrics": {"connected": 1}},
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
        self.assertIn('--control-email "$control_email"', supervisor)
        self.assertIn("platform_recover_retained_browser_report.py", cleanup)
        self.assertIn('--mode "$recovery_profile"', cleanup)
        self.assertIn("if SCRIPT_PATH in args and run_id in args:", abort)
