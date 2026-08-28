from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from apps.platform_api.app.api.routes.tournaments import (
    POLLING_DISABLED_MS,
    WORKSPACE_MANAGER_POLL_MS,
    WORKSPACE_PARTICIPANT_POLL_MS,
    tournament_workspace_poll_delay_ms,
)
from apps.platform_api.app.services.tournament_workflow import (
    ReadyCheckVoteWindowError,
    ensure_ready_check_vote_window,
)


class _Tournament:
    status = "registration_open"
    ready_check_ends_at = None
    captain_selection_starts_at = None


class PlatformReadyVoteWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.starts_at = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
        self.ends_at = self.starts_at + timedelta(minutes=10)

    def test_server_time_boundaries_are_exact(self) -> None:
        accepted = (
            self.starts_at,
            self.starts_at + timedelta(milliseconds=1),
            self.ends_at - timedelta(milliseconds=1),
        )
        for now in accepted:
            with self.subTest(now=now):
                ensure_ready_check_vote_window(
                    starts_at=self.starts_at,
                    ends_at=self.ends_at,
                    now=now,
                )

        for now in (
            self.starts_at - timedelta(milliseconds=1),
            self.ends_at,
            self.ends_at + timedelta(milliseconds=1),
        ):
            with self.subTest(now=now), self.assertRaises(ReadyCheckVoteWindowError):
                ensure_ready_check_vote_window(
                    starts_at=self.starts_at,
                    ends_at=self.ends_at,
                    now=now,
                )

    def test_missing_or_invalid_schedule_is_rejected(self) -> None:
        for starts_at, ends_at in (
            (None, self.ends_at),
            (self.starts_at, None),
            (self.starts_at, self.starts_at),
        ):
            with self.subTest(starts_at=starts_at, ends_at=ends_at):
                with self.assertRaises(ReadyCheckVoteWindowError):
                    ensure_ready_check_vote_window(
                        starts_at=starts_at,
                        ends_at=ends_at,
                        now=self.starts_at,
                    )

    def test_workspace_poll_budget_remains_for_generic_tournament_refreshes_only(self) -> None:
        tournament = _Tournament()
        self.assertEqual(
            tournament_workspace_poll_delay_ms(
                tournament,  # type: ignore[arg-type]
                has_participant_record=True,
                can_manage=False,
            ),
            WORKSPACE_PARTICIPANT_POLL_MS,
        )
        self.assertEqual(
            tournament_workspace_poll_delay_ms(
                tournament,  # type: ignore[arg-type]
                has_participant_record=False,
                can_manage=True,
            ),
            WORKSPACE_MANAGER_POLL_MS,
        )
        tournament.status = "completed"
        self.assertEqual(
            tournament_workspace_poll_delay_ms(
                tournament,  # type: ignore[arg-type]
                has_participant_record=True,
                can_manage=True,
            ),
            POLLING_DISABLED_MS,
        )


if __name__ == "__main__":
    unittest.main()
