from __future__ import annotations

import unittest

from python_packages.platform_domain.deadlock import (
    DreamSlotEditorError,
    build_captain_team_dream_slot_rows,
    build_captain_preview,
    captain_priority_bucket,
    validate_dream_slot_payload,
)


class PlatformDeadlockWebFlowTests(unittest.TestCase):
    def test_captain_priority_bucket_matches_product_rules(self):
        self.assertEqual(captain_priority_bucket("Eternus", "yes"), 0)
        self.assertEqual(captain_priority_bucket("Ascendant", "neutral"), 1)
        self.assertEqual(captain_priority_bucket("Ascendant", "no"), 2)
        self.assertEqual(captain_priority_bucket("Phantom", None), 1)
        self.assertEqual(captain_priority_bucket("Phantom", "no"), 2)

    def test_build_captain_preview_sorts_and_assigns_team_numbers(self):
        preview = build_captain_preview(
            [
                {
                    "user_id": "u3",
                    "display_name": "Neutral Ascendant",
                    "rank": "Ascendant",
                    "subrank": 1,
                    "playtime": "1001-1500",
                    "captain_priority": "neutral",
                    "strength": 10.0,
                },
                {
                    "user_id": "u1",
                    "display_name": "Yes Ascendant",
                    "rank": "Ascendant",
                    "subrank": 2,
                    "playtime": "1001-1500",
                    "captain_priority": "yes",
                    "strength": 11.0,
                },
                {
                    "user_id": "u2",
                    "display_name": "Neutral Phantom",
                    "rank": "Phantom",
                    "subrank": 4,
                    "playtime": "1001-1500",
                    "captain_priority": "neutral",
                    "strength": 12.0,
                },
            ],
            teams_count=2,
        )

        self.assertEqual([candidate.user_id for candidate in preview], ["u1", "u2"])
        self.assertEqual([candidate.projected_team_id for candidate in preview], ["2", "1"])

    def test_validate_dream_slot_payload_rejects_invalid_heroes(self):
        with self.assertRaises(DreamSlotEditorError):
            validate_dream_slot_payload(
                {
                    "slot_number": 1,
                    "allowed_roles": ["Carry"],
                    "desired_heroes": ["Abrams", "Apollo", "Kelvin", "Seven", "Ivy", "Mina"],
                }
            )

        with self.assertRaises(DreamSlotEditorError):
            validate_dream_slot_payload(
                {
                    "slot_number": 1,
                    "allowed_roles": ["Carry"],
                    "desired_heroes": ["Abrams", "NotAHero"],
                }
            )

        accepted = validate_dream_slot_payload(
            {
                "slot_number": 1,
                "allowed_roles": ["Carry"],
                "desired_heroes": ["Abrams", "Apollo", "Kelvin", "Seven", "Ivy"],
            }
        )
        self.assertEqual(len(accepted["desired_heroes"]), 5)

    def test_validate_dream_slot_payload_normalizes_allowed_roles(self):
        payload = validate_dream_slot_payload(
            {
                "slot_number": 2,
                "allowed_roles": ["Support", "Carry", "Carry", "BadRole"],
                "desired_heroes": ["Abrams"],
            }
        )

        self.assertEqual(payload["allowed_roles"], ["Carry", "Support"])
        self.assertEqual(payload["desired_heroes"], ["Abrams"])

    def test_validate_dream_slot_payload_accepts_source_discovered_hero(self):
        payload = validate_dream_slot_payload(
            {
                "slot_number": 1,
                "allowed_roles": ["Carry"],
                "desired_heroes": ["New Source Hero"],
            },
            supported_heroes={"New Source Hero"},
        )

        self.assertEqual(payload["desired_heroes"], ["New Source Hero"])

    def test_build_captain_team_dream_slot_rows_maps_captain_owned_drafts_to_assigned_teams(self):
        rows = build_captain_team_dream_slot_rows(
            [
                {"user_id": "captain-b", "team_id": "2"},
                {"user_id": "captain-a", "team_id": "1"},
            ],
            [
                {
                    "user_id": "captain-b",
                    "slot_number": 2,
                    "allowed_roles": ["Support"],
                    "desired_heroes": ["Kelvin"],
                },
                {
                    "user_id": "captain-a",
                    "slot_number": 1,
                    "allowed_roles": ["Carry"],
                    "desired_heroes": ["Abrams"],
                },
                {
                    "user_id": "someone-else",
                    "slot_number": 1,
                    "allowed_roles": ["Semi-Carry"],
                    "desired_heroes": ["Apollo"],
                },
            ],
        )

        self.assertEqual(
            list(rows),
            [
                {
                    "team_id": "1",
                    "slot_number": 1,
                    "allowed_roles": ["Carry"],
                    "desired_heroes": ["Abrams"],
                },
                {
                    "team_id": "2",
                    "slot_number": 2,
                    "allowed_roles": ["Support"],
                    "desired_heroes": ["Kelvin"],
                },
            ],
        )
