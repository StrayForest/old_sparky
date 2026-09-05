from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_api.app.services import deadlock_automation as automation
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class _RetrySession:
    def __init__(self, tournament: SimpleNamespace) -> None:
        self.tournament = tournament
        self.pending_artifacts: list[str] = []
        self.persisted_artifacts: list[str] = []
        self.persisted_ready_started_at = tournament.automation_ready_check_started_at
        self.persisted_last_error = tournament.automation_last_error
        self.persisted_failure_count = tournament.automation_failure_count
        self.persisted_retry_after = tournament.automation_retry_after

    async def scalar(self, _statement: object) -> SimpleNamespace:
        return self.tournament

    async def rollback(self) -> None:
        self.pending_artifacts.clear()
        self.tournament.automation_ready_check_started_at = (
            self.persisted_ready_started_at
        )
        self.tournament.automation_last_error = self.persisted_last_error
        self.tournament.automation_failure_count = self.persisted_failure_count
        self.tournament.automation_retry_after = self.persisted_retry_after

    async def commit(self) -> None:
        self.persisted_artifacts.extend(self.pending_artifacts)
        self.pending_artifacts.clear()
        self.persisted_ready_started_at = (
            self.tournament.automation_ready_check_started_at
        )
        self.persisted_last_error = self.tournament.automation_last_error
        self.persisted_failure_count = self.tournament.automation_failure_count
        self.persisted_retry_after = self.tournament.automation_retry_after


def _tournament(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "recovery-tournament",
        "slug": "recovery-tournament",
        "format_slug": "solo",
        "status": "registration_closed",
        "bracket_revision": 0,
        "registration_starts_at": None,
        "registration_closes_at": None,
        "ready_check_starts_at": None,
        "ready_check_ends_at": None,
        "captain_selection_starts_at": None,
        "captain_response_deadline_minutes": None,
        "automation_ready_check_started_at": None,
        "automation_ready_check_closed_at": None,
        "automation_captain_round_started_at": None,
        "automation_captain_round_finalized_at": None,
        "automation_assignment_generated_at": None,
        "automation_last_error": None,
        "automation_failure_count": 0,
        "automation_retry_after": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PlatformDeadlockAutomationRecoveryTests(PlatformIsolatedAsyncioTestCase):
    async def test_late_tick_catches_up_all_due_scheduled_transitions(self) -> None:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
        tournament = _tournament(
            status="registration_open",
            registration_starts_at=now - timedelta(hours=5),
            registration_closes_at=now - timedelta(hours=4),
            ready_check_starts_at=now - timedelta(hours=3),
            ready_check_ends_at=now - timedelta(hours=2),
            captain_selection_starts_at=now - timedelta(hours=1),
        )
        db_session = Mock()

        async def close_registration(
            _db_session: object,
            *,
            tournament: SimpleNamespace,
            now: datetime,
        ) -> bool:
            tournament.status = "registration_closed"
            return True

        async def start_ready(
            _db_session: object,
            *,
            tournament: SimpleNamespace,
            now: datetime,
        ) -> bool:
            tournament.automation_ready_check_started_at = now
            return True

        async def close_ready(
            _db_session: object,
            *,
            tournament: SimpleNamespace,
            now: datetime,
        ) -> bool:
            tournament.automation_ready_check_closed_at = now
            return True

        async def start_captains(
            _db_session: object,
            *,
            tournament: SimpleNamespace,
            now: datetime,
        ) -> bool:
            tournament.automation_captain_round_started_at = now
            return True

        async def recover_finalized_captains(
            _db_session: object,
            *,
            tournament: SimpleNamespace,
            now: datetime,
        ) -> bool:
            tournament.automation_captain_round_finalized_at = now
            return False

        async def generate_assignment(
            _db_session: object,
            *,
            tournament: SimpleNamespace,
            now: datetime,
        ) -> bool:
            tournament.automation_assignment_generated_at = now
            return True

        with (
            patch.object(
                automation,
                "tournament_has_locked_deadlock_roster",
                AsyncMock(return_value=False),
            ),
            patch.object(
                automation,
                "_ensure_registration_closed",
                AsyncMock(side_effect=close_registration),
            ) as ensure_registration_closed,
            patch.object(
                automation,
                "_ensure_ready_check_started",
                AsyncMock(side_effect=start_ready),
            ) as ensure_ready_started,
            patch.object(
                automation,
                "_ensure_ready_check_closed",
                AsyncMock(side_effect=close_ready),
            ) as ensure_ready_closed,
            patch.object(
                automation,
                "_ensure_captain_round_started",
                AsyncMock(side_effect=start_captains),
            ) as ensure_captain_started,
            patch.object(
                automation,
                "_expire_stale_captain_offers",
                AsyncMock(return_value=0),
            ),
            patch.object(
                automation,
                "_ensure_captain_round_finalized",
                AsyncMock(side_effect=recover_finalized_captains),
            ) as ensure_captain_finalized,
            patch.object(
                automation,
                "_ensure_assignment_generated",
                AsyncMock(side_effect=generate_assignment),
            ) as ensure_assignment_generated,
        ):
            result = await automation._advance_tournament(
                db_session,
                tournament=tournament,
                now=now,
            )

        self.assertEqual(result.registration_closed, 1)
        self.assertEqual(result.ready_started, 1)
        self.assertEqual(result.ready_closed, 1)
        self.assertEqual(result.captain_started, 1)
        self.assertEqual(result.assignment_generated, 1)
        ensure_registration_closed.assert_awaited_once()
        ensure_ready_started.assert_awaited_once()
        ensure_ready_closed.assert_awaited_once()
        ensure_captain_started.assert_awaited_once()
        ensure_captain_finalized.assert_awaited_once()
        ensure_assignment_generated.assert_awaited_once()

    async def test_crashed_uncommitted_attempt_rolls_back_and_retries_safely(
        self,
    ) -> None:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
        tournament = _tournament()
        db_session = _RetrySession(tournament)
        attempt_count = 0
        candidate = SimpleNamespace(
            tournament_id=tournament.id,
            due_at=now,
            workload_estimate=14,
            created_at=now - timedelta(days=1),
        )

        async def advance(
            session: _RetrySession,
            *,
            tournament: SimpleNamespace,
            now: datetime,
        ) -> automation.DeadlockAutomationResult:
            nonlocal attempt_count
            attempt_count += 1
            if tournament.automation_ready_check_started_at is not None:
                return automation.DeadlockAutomationResult()

            session.pending_artifacts.append("ready-round")
            tournament.automation_ready_check_started_at = now
            if attempt_count == 1:
                raise RuntimeError("simulated worker crash before commit")
            tournament.automation_last_error = None
            tournament.automation_failure_count = 0
            tournament.automation_retry_after = None
            return automation.DeadlockAutomationResult(ready_started=1)

        with (
            patch.object(
                automation,
                "_select_deadlock_automation_cohort",
                AsyncMock(return_value=([candidate], 0)),
            ),
            patch.object(automation, "_advance_tournament", side_effect=advance),
        ):
            crashed_result = await automation.run_deadlock_automation_tick(
                db_session,
                now=now,
                max_tournaments=1,
            )
            self.assertEqual(crashed_result.errors, 1)
            self.assertEqual(db_session.persisted_artifacts, [])
            self.assertIsNone(db_session.persisted_ready_started_at)
            self.assertEqual(db_session.persisted_failure_count, 1)
            self.assertEqual(
                db_session.persisted_retry_after,
                now + timedelta(minutes=1),
            )

            retry_result = await automation.run_deadlock_automation_tick(
                db_session,
                now=now + timedelta(minutes=1),
                max_tournaments=1,
            )
            rerun_result = await automation.run_deadlock_automation_tick(
                db_session,
                now=now + timedelta(minutes=2),
                max_tournaments=1,
            )

        self.assertEqual(retry_result.ready_started, 1)
        self.assertEqual(rerun_result, automation.DeadlockAutomationResult(scanned=1))
        self.assertEqual(db_session.persisted_artifacts, ["ready-round"])
        self.assertEqual(
            db_session.persisted_ready_started_at,
            now + timedelta(minutes=1),
        )

    async def test_persisted_ready_and_captain_rounds_restore_markers_without_duplicates(
        self,
    ) -> None:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
        tournament = _tournament()
        db_session = Mock()
        db_session.add = Mock()
        active_ready_round = SimpleNamespace(id=11, status="active")
        active_captain_round = SimpleNamespace(id=22, status="active")

        with patch.object(
            automation,
            "deadlock_ready_round_for_tournament",
            AsyncMock(return_value=active_ready_round),
        ):
            ready_created = await automation._ensure_ready_check_started(
                db_session,
                tournament=tournament,
                now=now,
            )

        with patch.object(
            automation,
            "deadlock_captain_round_for_tournament",
            AsyncMock(return_value=active_captain_round),
        ):
            captain_created = await automation._ensure_captain_round_started(
                db_session,
                tournament=tournament,
                now=now,
            )

        self.assertFalse(ready_created)
        self.assertFalse(captain_created)
        self.assertEqual(tournament.automation_ready_check_started_at, now)
        self.assertEqual(tournament.automation_captain_round_started_at, now)
        db_session.add.assert_not_called()

    async def test_fresh_persisted_assignment_restores_marker_without_duplicate_run(
        self,
    ) -> None:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
        persisted_at = now - timedelta(minutes=3)
        tournament = _tournament()
        db_session = Mock()
        db_session.add = Mock()
        captain_round = SimpleNamespace(
            id=22,
            source_ready_round_id=11,
            teams_count=2,
        )
        current_inputs = SimpleNamespace(
            captain_rows=(),
            ready_player_rows=(),
            dream_slot_rows=(),
            input_fingerprint="stable-inputs",
        )
        latest_run = SimpleNamespace(
            id="assignment-run",
            status="generated",
            created_at=persisted_at,
        )
        engine_factory = Mock()

        with (
            patch.object(
                automation,
                "deadlock_published_auto_assignment_run_for_tournament",
                AsyncMock(return_value=None),
            ),
            patch.object(
                automation,
                "deadlock_captain_round_for_tournament",
                AsyncMock(return_value=None),
            ),
            patch.object(
                automation,
                "deadlock_finalized_captain_round_for_tournament",
                AsyncMock(return_value=captain_round),
            ),
            patch.object(
                automation,
                "reconcile_finalized_captain_round_for_availability",
                AsyncMock(return_value=()),
            ) as reconcile_availability,
            patch.object(
                automation,
                "build_deadlock_auto_assignment_inputs",
                AsyncMock(return_value=current_inputs),
            ),
            patch.object(
                automation,
                "deadlock_auto_assignment_run_for_tournament",
                AsyncMock(return_value=latest_run),
            ),
            patch.object(
                automation,
                "deadlock_auto_assignment_run_freshness",
                AsyncMock(return_value=SimpleNamespace(is_stale=False)),
            ),
            patch.object(
                automation,
                "_ensure_assignment_handoff_completed",
                AsyncMock(return_value=False),
            ) as ensure_handoff,
            patch.object(
                automation,
                "AutoAssignmentEngine",
                engine_factory,
            ),
        ):
            created = await automation._ensure_assignment_generated(
                db_session,
                tournament=tournament,
                now=now,
            )

        self.assertFalse(created)
        self.assertEqual(
            tournament.automation_assignment_generated_at,
            persisted_at,
        )
        ensure_handoff.assert_awaited_once_with(
            db_session,
            tournament=tournament,
            run_row=latest_run,
            now=now,
        )
        reconcile_availability.assert_awaited_once_with(
            db_session,
            tournament=tournament,
            captain_round=captain_round,
            now=now,
        )
        engine_factory.assert_not_called()
        db_session.add.assert_not_called()

    async def test_persisted_matches_prevent_duplicate_bracket_on_rerun(self) -> None:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
        tournament = _tournament()
        locked_run = SimpleNamespace(id="assignment-run", status="locked")
        db_session = Mock()
        db_session.scalar = AsyncMock(return_value=1)

        with patch.object(
            automation,
            "create_full_bracket_graph",
            AsyncMock(),
        ) as create_bracket:
            changed = await automation._ensure_assignment_handoff_completed(
                db_session,
                tournament=tournament,
                run_row=locked_run,
                now=now,
            )

        self.assertFalse(changed)
        db_session.scalar.assert_awaited_once()
        create_bracket.assert_not_awaited()
