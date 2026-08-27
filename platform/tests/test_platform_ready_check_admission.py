from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from python_packages.platform_infra.ready_check_admission import (
    ReadyCheckAdmissionInvalid,
    issue_ready_check_state_proof,
    issue_ready_check_stream_proof,
    verify_ready_check_state_proof,
    verify_ready_check_stream_proof,
)


class PlatformReadyCheckAdmissionProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            platform_secret_key="test-ready-check-admission-secret-key-with-32-bytes!!",
        )
        self.now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

    def test_state_proof_round_trips_and_binds_session_and_slug(self) -> None:
        with patch(
            "python_packages.platform_infra.ready_check_admission.get_settings",
            return_value=self.settings,
        ):
            proof = issue_ready_check_state_proof(
                tournament_id="tournament-1",
                slug="ready-cup",
                user_id="user-1",
                session_id="session-1",
                session_token="session-token-1",
                ready_check_starts_at=self.now,
                ready_check_ends_at=self.now + timedelta(minutes=2),
                now=self.now,
            )
            verified = verify_ready_check_state_proof(
                proof,
                expected_slug="ready-cup",
                session_token="session-token-1",
                now=self.now + timedelta(seconds=1),
            )
            with self.assertRaises(ReadyCheckAdmissionInvalid):
                verify_ready_check_state_proof(
                    proof,
                    expected_slug="other-cup",
                    session_token="session-token-1",
                    now=self.now + timedelta(seconds=1),
                )
            with self.assertRaises(ReadyCheckAdmissionInvalid):
                verify_ready_check_state_proof(
                    proof,
                    expected_slug="ready-cup",
                    session_token="session-token-2",
                    now=self.now + timedelta(seconds=1),
                )

        self.assertEqual(verified.tournament_id, "tournament-1")
        self.assertEqual(verified.user_id, "user-1")

    def test_stream_proof_rejects_tampering_and_unbounded_tournament_set(self) -> None:
        with patch(
            "python_packages.platform_infra.ready_check_admission.get_settings",
            return_value=self.settings,
        ):
            proof = issue_ready_check_stream_proof(
                user_id="user-1",
                session_id="session-1",
                session_token="session-token-1",
                tournament_ids=["tournament-1", "tournament-2", "tournament-1"],
                admission_open_at=self.now + timedelta(minutes=1),
                now=self.now,
            )
            verified = verify_ready_check_stream_proof(
                proof,
                session_token="session-token-1",
                now=self.now + timedelta(seconds=1),
            )
            with self.assertRaises(ReadyCheckAdmissionInvalid):
                verify_ready_check_stream_proof(
                    proof[:-1] + ("a" if proof[-1] != "a" else "b"),
                    session_token="session-token-1",
                    now=self.now + timedelta(seconds=1),
                )
            with self.assertRaises(ValueError):
                issue_ready_check_stream_proof(
                    user_id="user-1",
                    session_id="session-1",
                    session_token="session-token-1",
                    tournament_ids=[str(index) for index in range(129)],
                    now=self.now,
                )

        self.assertEqual(verified.tournament_ids, ("tournament-1", "tournament-2"))
        self.assertEqual(verified.admission_open_at, int((self.now + timedelta(minutes=1)).timestamp()))
