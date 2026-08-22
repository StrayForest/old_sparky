from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from apps.platform_api.app.api.routes.tournaments import (
    POLLING_DISABLED_MS,
    READY_CHECK_ACTIVE_PARTICIPANT_POLL_MS,
    READY_CHECK_ACTIVE_VIEWER_POLL_MS,
    WORKSPACE_MANAGER_POLL_MS,
    WORKSPACE_PARTICIPANT_POLL_MS,
    ready_check_poll_delay_ms,
    tournament_workspace_poll_delay_ms,
)
from apps.platform_api.app.services.tournament_workflow import ready_vote_requires_automation
from apps.platform_api.app.api.schemas import TournamentDeadlockReadyRoundResponse


class _Tournament:
    status = "registration_open"
    ready_check_ends_at = None
    captain_selection_starts_at = None


class _ReadyRound:
    pass


class PlatformReadyVotePerformanceTests(unittest.TestCase):
    def test_active_round_before_deadlines_skips_automation(self) -> None:
        now = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
        tournament = _Tournament()
        tournament.ready_check_ends_at = now + timedelta(minutes=10)
        tournament.captain_selection_starts_at = now + timedelta(minutes=20)

        self.assertFalse(
            ready_vote_requires_automation(
                tournament,  # type: ignore[arg-type]
                _ReadyRound(),  # type: ignore[arg-type]
                now=now,
            )
        )

    def test_missing_round_requires_automation(self) -> None:
        now = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)

        self.assertTrue(
            ready_vote_requires_automation(
                _Tournament(),  # type: ignore[arg-type]
                None,
                now=now,
            )
        )

    def test_elapsed_ready_deadline_requires_automation(self) -> None:
        now = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
        tournament = _Tournament()
        tournament.ready_check_ends_at = now

        self.assertTrue(
            ready_vote_requires_automation(
                tournament,  # type: ignore[arg-type]
                _ReadyRound(),  # type: ignore[arg-type]
                now=now,
            )
        )

    def test_elapsed_captain_deadline_requires_automation(self) -> None:
        now = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
        tournament = _Tournament()
        tournament.captain_selection_starts_at = now

        self.assertTrue(
            ready_vote_requires_automation(
                tournament,  # type: ignore[arg-type]
                _ReadyRound(),  # type: ignore[arg-type]
                now=now,
            )
        )

    def test_ready_check_poll_budget_uses_active_role_and_turns_off_closed_state(self) -> None:
        now = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
        active_round = TournamentDeadlockReadyRoundResponse(
            id=42,
            tournament_id="tournament",
            status="active",
            eligible_participant_count=2,
            ready_count=1,
            declined_count=0,
            initiated_by_user_id=None,
            created_at=now,
            closed_at=None,
        )
        closed_round = TournamentDeadlockReadyRoundResponse(
            id=42,
            tournament_id="tournament",
            status="closed",
            eligible_participant_count=2,
            ready_count=2,
            declined_count=0,
            initiated_by_user_id=None,
            created_at=now,
            closed_at=now,
        )

        self.assertEqual(
            ready_check_poll_delay_ms(
                active_round=active_round,
                latest_round=active_round,
                has_participant_context=True,
            ),
            READY_CHECK_ACTIVE_PARTICIPANT_POLL_MS,
        )
        self.assertEqual(
            ready_check_poll_delay_ms(
                active_round=active_round,
                latest_round=active_round,
                has_participant_context=False,
            ),
            READY_CHECK_ACTIVE_VIEWER_POLL_MS,
        )
        self.assertEqual(
            ready_check_poll_delay_ms(
                active_round=None,
                latest_round=closed_round,
                has_participant_context=True,
            ),
            POLLING_DISABLED_MS,
        )

    def test_workspace_poll_budget_is_role_aware_and_terminal_safe(self) -> None:
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
