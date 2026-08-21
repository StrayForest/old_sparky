from __future__ import annotations

import unittest

from tools.platform_auto_assignment_benchmark import (
    DEFAULT_TEAM_COUNTS,
    EXPECTED_SNAPSHOT_DIGESTS,
    READY_PLAYERS_PER_TEAM,
    benchmark_fixture,
    build_fixture,
    snapshot_digest,
    solve_fixture,
)


class PlatformAutoAssignmentBenchmarkTests(unittest.TestCase):
    def test_fixture_matches_supported_team_shape(self) -> None:
        fixture = build_fixture(4)

        self.assertEqual(len(fixture.captain_rows), 4)
        self.assertEqual(
            len(fixture.ready_player_rows),
            4 * READY_PLAYERS_PER_TEAM,
        )
        self.assertEqual(
            len(fixture.dream_slot_rows),
            4 * READY_PLAYERS_PER_TEAM,
        )

    def test_fixture_solver_snapshot_is_deterministic(self) -> None:
        fixture = build_fixture(2)

        first_digest, first_snapshot = solve_fixture(fixture)
        second_digest, second_snapshot = solve_fixture(fixture)

        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_snapshot, second_snapshot)
        self.assertEqual(first_digest, snapshot_digest(first_snapshot))
        self.assertEqual(
            first_digest,
            EXPECTED_SNAPSHOT_DIGESTS[2],
        )
        self.assertEqual(len(first_snapshot["teams"]), 2)

    def test_default_fixtures_match_golden_snapshots(self) -> None:
        for teams_count in DEFAULT_TEAM_COUNTS:
            with self.subTest(teams_count=teams_count):
                digest, _ = solve_fixture(build_fixture(teams_count))

                self.assertEqual(
                    digest,
                    EXPECTED_SNAPSHOT_DIGESTS[teams_count],
                )

    def test_fixture_rejects_unsupported_team_count(self) -> None:
        with self.assertRaises(ValueError):
            build_fixture(3)

    def test_default_team_counts_avoid_expensive_capacity_scenarios(self) -> None:
        self.assertEqual(DEFAULT_TEAM_COUNTS, (2, 4, 8, 16))

    def test_memory_measurement_can_be_skipped(self) -> None:
        result = benchmark_fixture(
            2,
            iterations=1,
            warmup=0,
            measure_memory=False,
        )

        self.assertIsNone(result.traced_peak_mib)
