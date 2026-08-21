from __future__ import annotations

import unittest

from python_packages.platform_domain.deadlock import (
    RegistrationPayload,
    RegistrationValidationError,
    prepare_ready_check_start,
    calculate_player_strength,
    validate_registration_payload,
)


class PlatformDomainTests(unittest.TestCase):
    def test_strength_bonus_is_smaller_than_subrank_step(self):
        low_hours = calculate_player_strength("Phantom", 2, "0-500")
        high_hours = calculate_player_strength("Phantom", 2, "3000+")
        next_subrank = calculate_player_strength("Phantom", 3, "0-500")

        self.assertGreater(high_hours, low_hours)
        self.assertLess(high_hours - low_hours, next_subrank - low_hours)

    def test_registration_payload_requires_valid_rank(self):
        payload = RegistrationPayload(
            rank="Bad Rank",
            subrank=2,
            playtime="1001-1500",
            roles=["Carry"],
        )

        with self.assertRaises(RegistrationValidationError):
            validate_registration_payload(payload)

    def test_registration_payload_accepts_high_rank_captain_priority(self):
        payload = RegistrationPayload(
            rank="Eternus",
            subrank=2,
            playtime="1001-1500",
            roles=["Carry"],
            captain_priority="yes",
        )

        validate_registration_payload(payload)

    def test_ready_check_start_builder_rejects_empty_user_pool(self):
        decision = prepare_ready_check_start([], has_active_round=False)

        self.assertEqual(decision.status, "empty")
        self.assertFalse(decision.should_create_round)
