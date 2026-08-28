from __future__ import annotations

import unittest
from pathlib import Path

from tools.platform_distributed_ready_check_sse import (
    aggregate_shard_markers,
    latency_stats,
    validate_numeric,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class DistributedReadyCheckHarnessTests(unittest.TestCase):
    def marker(self, shard: int, ip: str, *, connected: int = 20) -> dict:
        return {
            "run_id": 123,
            "shard": shard,
            "status": "ready",
            "attempted": 20,
            "connected": connected,
            "agenda_attempted": 20,
            "agenda_successful": 20,
            "egress": {"ip": ip, "colo": "HEL"},
            "response_statuses": {"200": connected},
        }

    def test_aggregate_requires_unique_egress_for_each_shard(self) -> None:
        markers = [self.marker(index, f"198.51.100.{index + 1}") for index in range(5)]
        result = aggregate_shard_markers(markers, target=100, triggered=True)
        self.assertTrue(result["independent_egress"])
        self.assertEqual(result["connected"], 100)

        duplicate = [self.marker(index, "198.51.100.1") for index in range(5)]
        self.assertFalse(
            aggregate_shard_markers(duplicate, target=100, triggered=True)[
                "independent_egress"
            ]
        )

    def test_latency_stats_exposes_capacity_gate_percentiles_and_max(self) -> None:
        self.assertEqual(
            latency_stats([1.0, 2.0, 3.0, 4.0]),
            {"count": 4, "p50": 2.5, "p95": 3.85, "p99": 3.97, "max": 4.0},
        )

    def test_accepts_current_github_run_id_range(self) -> None:
        self.assertEqual(validate_numeric("33159181256", name="run_id"), 33159181256)

    def test_workflow_keeps_external_shards_and_production_cap_guarded(self) -> None:
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-distributed-ready-check-sse.yml"
        ).read_text(encoding="utf-8")
        coordinator = (
            REPO_ROOT
            / "platform/tools/platform_distributed_ready_check_sse.py"
        ).read_text(encoding="utf-8")
        self.assertIn("matrix:", workflow)
        self.assertIn("shard: [0, 1, 2, 3, 4]", workflow)
        self.assertIn("aggregate_open_rate=25", workflow)
        self.assertIn("shard_open_rate=5", workflow)
        self.assertIn("github-hosted-runners", coordinator)
        self.assertIn("RUN-PRODUCTION-DISTRIBUTED-READY-CHECK-SSE", workflow)
        self.assertIn("capacity-5000", workflow)
        self.assertIn("platform_distributed_ready_check_files.py", workflow)
        self.assertIn("DELETE-PRODUCTION-RETAINED-LOAD", workflow)
        self.assertNotIn("READY_CHECK_SSE_GLOBAL_LIMIT=10000", workflow)


if __name__ == "__main__":
    unittest.main()
