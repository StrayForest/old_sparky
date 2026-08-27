from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from apps.platform_api.app.api.schemas import AdminTournamentOverrideRequest, TournamentCreateRequest
from python_packages.platform_infra.ready_check_admission import (
    READY_CHECK_ADMISSION_MAX_TTL_SECONDS,
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
                ready_check_ends_at=self.now + timedelta(minutes=30),
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
        self.assertEqual(verified.ready_check_ends_at, int((self.now + timedelta(minutes=30)).timestamp()))

    def test_state_proof_remains_valid_past_the_old_fifteen_minute_boundary(self) -> None:
        with patch(
            "python_packages.platform_infra.ready_check_admission.get_settings",
            return_value=self.settings,
        ):
            proof = issue_ready_check_state_proof(
                tournament_id="tournament-long",
                slug="long-cup",
                user_id="user-1",
                session_id="session-1",
                session_token="session-token-1",
                ready_check_starts_at=self.now,
                ready_check_ends_at=self.now + timedelta(minutes=30),
                now=self.now,
            )
            verified = verify_ready_check_state_proof(
                proof,
                expected_slug="long-cup",
                session_token="session-token-1",
                now=self.now + timedelta(minutes=15, seconds=1),
            )

        self.assertEqual(
            verified.expires_at,
            int((self.now + timedelta(minutes=30, seconds=5)).timestamp()),
        )

    def test_stream_reconnect_proof_remains_valid_past_the_old_fifteen_minute_boundary(self) -> None:
        with patch(
            "python_packages.platform_infra.ready_check_admission.get_settings",
            return_value=self.settings,
        ):
            proof = issue_ready_check_stream_proof(
                user_id="user-1",
                session_id="session-1",
                session_token="session-token-1",
                tournament_ids=["tournament-long"],
                admission_open_at=self.now,
                ready_check_ends_at=self.now + timedelta(minutes=30),
                now=self.now,
            )
            verified = verify_ready_check_stream_proof(
                proof,
                session_token="session-token-1",
                now=self.now + timedelta(minutes=15, seconds=1),
            )

        self.assertEqual(
            verified.expires_at,
            int((self.now + timedelta(minutes=30, seconds=5)).timestamp()),
        )

    def test_stream_proof_has_a_bounded_horizon_for_invalid_long_windows(self) -> None:
        with patch(
            "python_packages.platform_infra.ready_check_admission.get_settings",
            return_value=self.settings,
        ):
            proof = issue_ready_check_stream_proof(
                user_id="user-1",
                session_id="session-1",
                session_token="session-token-1",
                tournament_ids=["tournament-long"],
                admission_open_at=self.now,
                ready_check_ends_at=self.now + timedelta(days=3),
                now=self.now,
            )
            verified = verify_ready_check_stream_proof(
                proof,
                session_token="session-token-1",
                now=self.now + timedelta(seconds=READY_CHECK_ADMISSION_MAX_TTL_SECONDS - 1),
            )
            with self.assertRaises(ReadyCheckAdmissionInvalid):
                verify_ready_check_stream_proof(
                    proof,
                    session_token="session-token-1",
                    now=self.now + timedelta(seconds=READY_CHECK_ADMISSION_MAX_TTL_SECONDS),
                )

        self.assertEqual(
            verified.expires_at,
            int((self.now + timedelta(seconds=READY_CHECK_ADMISSION_MAX_TTL_SECONDS)).timestamp()),
        )

    def test_tournament_schedule_rejects_a_ready_check_longer_than_the_supported_window(self) -> None:
        schedule = {
            "registration_closes_at": self.now,
            "ready_check_starts_at": self.now,
            "ready_check_ends_at": self.now + timedelta(days=1, seconds=1),
            "captain_selection_starts_at": self.now + timedelta(days=1, minutes=1),
            "starts_at": self.now + timedelta(days=1, minutes=2),
        }
        with self.assertRaises(ValueError):
            TournamentCreateRequest(name="Long Ready Check", **schedule)
        with self.assertRaises(ValueError):
            AdminTournamentOverrideRequest(status="registration_open", **schedule)
