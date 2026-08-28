from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.platform_seed_retained_tournament_matrix import (
    INVITE_MAX_USERS,
    allocate_user_counts,
    compact_performance,
    matrix_plan,
    summarize_matrix_performance,
    validate_matrix_plan,
)


class RetainedTournamentMatrixTests(unittest.TestCase):
    def test_plan_is_ten_thousand_users_with_one_control_assignment(self) -> None:
        plan = matrix_plan()
        validate_matrix_plan(plan)

        self.assertEqual(len(plan), 20)
        counts = allocate_user_counts(plan, users_per_tournament=500)
        self.assertEqual(sum(counts), 10_000)
        self.assertEqual(counts[15], 448)
        self.assertEqual(min(counts), 56)
        self.assertEqual(max(counts), 528)
        self.assertEqual(counts[15], 64 * 7)
        self.assertTrue(
            all(
                count <= INVITE_MAX_USERS
                for count, item in zip(counts, plan)
                if item["visibility"] == "invite_only"
            )
        )
        self.assertEqual(sum(item["teams"] for item in plan), 600)
        self.assertEqual(
            sum(item["control_state"] == "assigned" for item in plan),
            1,
        )
        self.assertEqual(
            {item["visibility"] for item in plan},
            {"public", "invite_only"},
        )
        self.assertEqual(
            sum(item["visibility"] == "invite_only" for item in plan),
            1,
        )
        self.assertEqual(
            [(item["teams"], item["control_state"]) for item in plan if item["control_state"] == "assigned"],
            [(64, "assigned")],
        )

    def test_compact_performance_keeps_bottleneck_evidence_without_fixture_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "user_ids": ["must-not-be-copied"],
                        "performance": {
                            "http_client": {"overall": {"p95_ms": 123}},
                            "bottleneck_summary": {
                                "likely_bottleneck_classes": ["cpu"],
                                "resource_flags": {"cpu_saturated": True},
                                "top_client_phases_by_p95": [
                                    {"name": "profile", "p95_ms": 123, "p99_ms": 150}
                                ],
                                "top_server_routes_by_p95": [
                                    {"route": "/profiles/me", "p95_ms": 120}
                                ],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            compact = compact_performance(report_path)

        self.assertEqual(compact["http_overall"]["p95_ms"], 123)
        self.assertEqual(compact["likely_bottleneck_classes"], ["cpu"])
        self.assertEqual(compact["top_server_routes_by_p95"][0]["route"], "/profiles/me")
        self.assertNotIn("user_ids", compact)

    def test_matrix_performance_summary_rolls_up_safe_bottleneck_evidence(self) -> None:
        summary = summarize_matrix_performance(
            [
                {
                    "performance": {
                        "http_overall": {"p95_ms": 100, "p99_ms": 150},
                        "likely_bottleneck_classes": ["cpu"],
                        "resource_flags": {"cpu_saturated": True},
                        "top_client_phases_by_p95": [
                            {"name": "assignment", "p95_ms": 900}
                        ],
                        "top_server_routes_by_p95": [
                            {"route": "/tournaments/{slug}", "p95_ms": 700}
                        ],
                    }
                },
                {
                    "performance": {
                        "http_overall": {"p95_ms": 200, "p99_ms": 250},
                        "likely_bottleneck_classes": ["cpu"],
                        "resource_flags": {"cpu_saturated": True},
                        "top_client_phases_by_p95": [
                            {"name": "assignment", "p95_ms": 1100}
                        ],
                        "top_server_routes_by_p95": [],
                    }
                },
            ]
        )

        self.assertEqual(summary["runs_with_performance"], 2)
        self.assertEqual(summary["worst_http_p95_ms"], 200)
        self.assertEqual(summary["bottleneck_classes"], [{"class": "cpu", "runs": 2}])
        self.assertEqual(summary["top_client_phases_by_worst_p95"][0]["name"], "assignment")
        self.assertNotIn("user_ids", summary)


if __name__ == "__main__":
    unittest.main()
