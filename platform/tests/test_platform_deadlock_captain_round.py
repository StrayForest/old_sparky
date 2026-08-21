from __future__ import annotations

import unittest

from python_packages.platform_domain.deadlock import (
    prepare_captain_round_entries,
)


class PlatformDeadlockCaptainRoundTests(unittest.TestCase):
    @staticmethod
    def _candidate_rows() -> list[dict[str, object]]:
        return [
            {
                "user_id": "u1",
                "rank": "Ascendant",
                "subrank": 2,
                "playtime": "1501-2000",
                "captain_priority_bucket": 0,
                "strength": 12.0,
            },
            {
                "user_id": "u2",
                "rank": "Ascendant",
                "subrank": 4,
                "playtime": "1001-1500",
                "captain_priority_bucket": 0,
                "strength": 11.2,
            },
            {
                "user_id": "u3",
                "rank": "Phantom",
                "subrank": 1,
                "playtime": "3000+",
                "captain_priority_bucket": 1,
                "strength": 13.7,
            },
            {
                "user_id": "u4",
                "rank": "Phantom",
                "subrank": 3,
                "playtime": "2001-3000",
                "captain_priority_bucket": 1,
                "strength": 13.1,
            },
            {
                "user_id": "u5",
                "rank": "Oracle",
                "subrank": 6,
                "playtime": "1501-2000",
                "captain_priority_bucket": 2,
                "strength": 15.8,
            },
        ]

    def test_prepare_captain_round_entries_orders_priority_then_strength(self):
        entries = prepare_captain_round_entries(self._candidate_rows(), teams_count=2)

        self.assertEqual(
            [(entry.user_id, entry.state, entry.offer_order) for entry in entries],
            [
                ("u1", "offered", 1),
                ("u2", "offered", 2),
                ("u3", "queued", 3),
                ("u4", "queued", 4),
                ("u5", "queued", 5),
            ],
        )

    def test_auto_assign_immediately_assigns_top_captains_and_queues_rest(self):
        entries = prepare_captain_round_entries(
            self._candidate_rows(),
            teams_count=4,
            auto_assign=True,
        )

        self.assertEqual(
            [(entry.user_id, entry.state, entry.assigned_team_id) for entry in entries],
            [
                ("u1", "assigned", "3"),
                ("u2", "assigned", "4"),
                ("u3", "assigned", "1"),
                ("u4", "assigned", "2"),
                ("u5", "queued", None),
            ],
        )

    def test_auto_assign_falls_back_to_neutral_and_no_when_priority_pool_is_short(self):
        entries = prepare_captain_round_entries(
            self._candidate_rows(),
            teams_count=5,
            auto_assign=True,
        )

        self.assertEqual(
            [entry.user_id for entry in entries if entry.state == "assigned"],
            ["u1", "u2", "u3", "u4", "u5"],
        )
