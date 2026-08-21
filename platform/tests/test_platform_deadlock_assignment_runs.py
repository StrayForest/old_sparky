from __future__ import annotations

import unittest

from python_packages.platform_domain.deadlock import (
    AutoAssignmentRunWorkflowError,
    build_auto_assignment_input_fingerprint,
    evaluate_auto_assignment_run_freshness,
    next_auto_assignment_run_statuses,
    transition_auto_assignment_run_status,
)


class PlatformDeadlockAssignmentRunWorkflowTests(unittest.TestCase):
    def test_build_input_fingerprint_normalizes_and_sorts_rows(self):
        fingerprint = build_auto_assignment_input_fingerprint(
            [
                {
                    "user_id": "captain-b",
                    "team_id": "2",
                    "rank": "Ascendant",
                    "subrank": 3,
                    "team_name": "Bravo",
                    "playtime": "1001-1500",
                    "pool": ["Kelvin", "Abrams"],
                    "roles": ["Support", "Carry", "Carry"],
                },
                {
                    "user_id": "captain-a",
                    "team_id": "1",
                    "rank": "Eternus",
                    "subrank": 1,
                    "team_name": "Alpha",
                    "playtime": "3000+",
                    "pool": ["Haze"],
                    "roles": ["Carry"],
                },
            ],
            [
                {
                    "user_id": "player-b",
                    "rank": "Phantom",
                    "subrank": 2,
                    "playtime": "501-1000",
                    "pool": ["Seven", "Abrams"],
                    "roles": ["Support", "Semi-Support"],
                },
                {
                    "user_id": "player-a",
                    "rank": "Oracle",
                    "subrank": 5,
                    "playtime": "1001-1500",
                    "pool": ["Vindicta", "Warden", "Vindicta"],
                    "roles": ["Semi-Carry", "Carry"],
                },
            ],
            [
                {
                    "team_id": "2",
                    "slot_number": 2,
                    "allowed_roles": ["Support", "Carry"],
                    "desired_heroes": ["Kelvin", "Abrams"],
                },
                {
                    "team_id": "1",
                    "slot_number": 1,
                    "allowed_roles": ["Carry", "Carry"],
                    "desired_heroes": ["Haze", "Abrams"],
                },
            ],
        )

        self.assertEqual(
            fingerprint,
            {
                "captains": [
                    {
                        "user_id": "captain-a",
                        "team_id": "1",
                        "team_name": "Alpha",
                        "rank": "Eternus",
                        "subrank": 1,
                        "playtime": "3000+",
                        "pool": ["Haze"],
                        "roles": ["Carry"],
                    },
                    {
                        "user_id": "captain-b",
                        "team_id": "2",
                        "team_name": "Bravo",
                        "rank": "Ascendant",
                        "subrank": 3,
                        "playtime": "1001-1500",
                        "pool": ["Abrams", "Kelvin"],
                        "roles": ["Carry", "Support"],
                    },
                ],
                "ready_players": [
                    {
                        "user_id": "player-a",
                        "rank": "Oracle",
                        "subrank": 5,
                        "playtime": "1001-1500",
                        "pool": ["Vindicta", "Warden"],
                        "roles": ["Carry", "Semi-Carry"],
                    },
                    {
                        "user_id": "player-b",
                        "rank": "Phantom",
                        "subrank": 2,
                        "playtime": "501-1000",
                        "pool": ["Abrams", "Seven"],
                        "roles": ["Semi-Support", "Support"],
                    },
                ],
                "dream_slots": [
                    {
                        "team_id": "1",
                        "slot_number": 1,
                        "allowed_roles": ["Carry"],
                        "desired_heroes": ["Abrams", "Haze"],
                    },
                    {
                        "team_id": "2",
                        "slot_number": 2,
                        "allowed_roles": ["Carry", "Support"],
                        "desired_heroes": ["Abrams", "Kelvin"],
                    },
                ],
            },
        )

    def test_identical_inputs_are_not_stale(self):
        fingerprint = build_auto_assignment_input_fingerprint(
            [
                {
                    "user_id": "captain-a",
                    "team_id": "1",
                    "rank": "Eternus",
                    "subrank": 1,
                    "playtime": "3000+",
                    "pool": ["Haze"],
                    "roles": ["Carry"],
                }
            ],
            [
                {
                    "user_id": "player-a",
                    "rank": "Oracle",
                    "subrank": 5,
                    "playtime": "1001-1500",
                    "pool": ["Vindicta"],
                    "roles": ["Carry"],
                }
            ],
            [
                {
                    "team_id": "1",
                    "slot_number": 1,
                    "allowed_roles": ["Carry"],
                    "desired_heroes": ["Haze"],
                }
            ],
        )

        freshness = evaluate_auto_assignment_run_freshness(
            run_source_captain_round_id=12,
            current_source_captain_round_id=12,
            run_source_ready_round_id=8,
            current_source_ready_round_id=8,
            stored_input_fingerprint=fingerprint,
            current_input_fingerprint=fingerprint,
        )

        self.assertFalse(freshness.is_stale)
        self.assertEqual(freshness.stale_reasons, ())

    def test_changed_inputs_mark_run_stale(self):
        stored_fingerprint = build_auto_assignment_input_fingerprint(
            [
                {
                    "user_id": "captain-a",
                    "team_id": "1",
                    "rank": "Eternus",
                    "subrank": 1,
                    "playtime": "3000+",
                    "pool": ["Haze"],
                    "roles": ["Carry"],
                }
            ],
            [
                {
                    "user_id": "player-a",
                    "rank": "Oracle",
                    "subrank": 5,
                    "playtime": "1001-1500",
                    "pool": ["Vindicta"],
                    "roles": ["Carry"],
                }
            ],
            [
                {
                    "team_id": "1",
                    "slot_number": 1,
                    "allowed_roles": ["Carry"],
                    "desired_heroes": ["Haze"],
                }
            ],
        )
        current_fingerprint = build_auto_assignment_input_fingerprint(
            [
                {
                    "user_id": "captain-b",
                    "team_id": "1",
                    "rank": "Ascendant",
                    "subrank": 3,
                    "playtime": "1001-1500",
                    "pool": ["Kelvin"],
                    "roles": ["Support"],
                }
            ],
            [
                {
                    "user_id": "player-a",
                    "rank": "Oracle",
                    "subrank": 5,
                    "playtime": "1001-1500",
                    "pool": ["Vindicta"],
                    "roles": ["Carry"],
                },
                {
                    "user_id": "player-b",
                    "rank": "Phantom",
                    "subrank": 2,
                    "playtime": "501-1000",
                    "pool": ["Abrams"],
                    "roles": ["Support"],
                },
            ],
            [
                {
                    "team_id": "1",
                    "slot_number": 1,
                    "allowed_roles": ["Support"],
                    "desired_heroes": ["Kelvin"],
                }
            ],
        )

        freshness = evaluate_auto_assignment_run_freshness(
            run_source_captain_round_id=12,
            current_source_captain_round_id=13,
            run_source_ready_round_id=8,
            current_source_ready_round_id=9,
            stored_input_fingerprint=stored_fingerprint,
            current_input_fingerprint=current_fingerprint,
        )

        self.assertTrue(freshness.is_stale)
        self.assertEqual(
            freshness.stale_reasons,
            (
                "captain_round_changed",
                "ready_round_changed",
                "captains_changed",
                "ready_players_changed",
                "dream_slots_changed",
            ),
        )

    def test_generated_run_can_be_published(self):
        self.assertEqual(next_auto_assignment_run_statuses("generated"), ("published",))
        self.assertEqual(
            transition_auto_assignment_run_status("generated", "published"),
            "published",
        )

    def test_published_run_can_be_locked_or_superseded(self):
        self.assertEqual(
            next_auto_assignment_run_statuses("published"),
            ("superseded", "locked"),
        )
        self.assertEqual(
            transition_auto_assignment_run_status("published", "locked"),
            "locked",
        )
        self.assertEqual(
            transition_auto_assignment_run_status("published", "superseded"),
            "superseded",
        )

    def test_superseded_run_can_be_republished(self):
        self.assertEqual(next_auto_assignment_run_statuses("superseded"), ("published",))
        self.assertEqual(
            transition_auto_assignment_run_status("superseded", "published"),
            "published",
        )

    def test_locked_run_rejects_further_transitions(self):
        self.assertEqual(next_auto_assignment_run_statuses("locked"), ())
        with self.assertRaises(AutoAssignmentRunWorkflowError):
            transition_auto_assignment_run_status("locked", "published")

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(AutoAssignmentRunWorkflowError):
            next_auto_assignment_run_statuses("broken")
