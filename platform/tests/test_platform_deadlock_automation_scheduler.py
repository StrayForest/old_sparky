from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from apps.platform_api.app.services import deadlock_automation
from apps.platform_api.app.services.deadlock_automation import (
    DeadlockAutomationResult,
    _deadlock_automation_cohort_statement,
    run_deadlock_automation_tick,
)
from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.models import Tournament


def compiled_sql(statement) -> str:
    return " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )


class PlatformDeadlockAutomationSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_cohort_query_is_due_only_fifo_bounded_and_excludes_finished_rows(self) -> None:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)

        sql = compiled_sql(
            _deadlock_automation_cohort_statement(now=now, limit=4)
        )
        final_order = sql.rsplit(" ORDER BY ", 1)[1]

        self.assertIn("LIMIT 4", sql)
        self.assertIn("SELECT count(*) AS count_1 FROM automation_candidates", sql)
        self.assertIn("automation_candidates.due_at IS NOT NULL", sql)
        self.assertNotIn("date_trunc", sql)
        self.assertIn("automation_retry_after IS NULL", sql)
        self.assertIn("automation_retry_after <=", sql)
        self.assertLess(
            sql.index("LIMIT 4"),
            sql.index("active_participant_counts AS"),
        )
        self.assertIn("registration_starts_at <=", sql)
        self.assertIn("registration_closes_at <=", sql)
        self.assertIn("ready_check_starts_at <=", sql)
        self.assertIn("ready_check_ends_at <=", sql)
        self.assertIn("captain_selection_starts_at <=", sql)
        self.assertIn("automation_assignment_generated_at IS NULL", sql)
        self.assertIn("tournament_deadlock_assignment_runs.status = 'locked'", sql)
        self.assertIn("NOT (EXISTS", sql)
        self.assertIn(
            "coalesce(nullif(active_participant_counts.active_participant_count, 0), "
            "selected_automation_cohort.teams_count * 6, 0) AS workload_estimate",
            sql,
        )
        self.assertTrue(
            final_order.startswith(
                "selected_automation_cohort.due_at ASC, "
                "selected_automation_cohort.created_at ASC, "
                "selected_automation_cohort.tournament_id ASC"
            )
        )
        self.assertNotIn("workload_estimate", final_order)

    def test_scheduler_limit_defaults_to_four_and_must_be_positive(self) -> None:
        settings = PlatformSettings(_env_file=None)
        self.assertEqual(
            settings.platform_deadlock_automation_max_tournaments_per_tick,
            4,
        )

        with self.assertRaises(ValidationError):
            PlatformSettings(
                _env_file=None,
                platform_deadlock_automation_max_tournaments_per_tick=0,
            )

    async def test_due_window_runs_small_first_with_stable_ties(self) -> None:
        now = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
        created_at = now - timedelta(days=1)
        cohort_due_at = now - timedelta(hours=3)
        cohort_result = Mock()
        cohort_result.mappings.return_value.all.return_value = [
            {
                "tournament_id": "old-large",
                "due_at": cohort_due_at,
                "workload_estimate": 24,
                "created_at": created_at,
                "candidate_count": 14,
            },
            {
                "tournament_id": "old-small-a",
                "due_at": cohort_due_at + timedelta(seconds=10),
                "workload_estimate": 6,
                "created_at": created_at,
                "candidate_count": 14,
            },
            {
                "tournament_id": "old-small-z",
                "due_at": cohort_due_at + timedelta(seconds=20),
                "workload_estimate": 6,
                "created_at": created_at,
                "candidate_count": 14,
            },
        ]
        tournaments = [
            Tournament(id="old-small-a", bracket_revision=0),
            Tournament(id="old-small-z", bracket_revision=0),
            Tournament(id="old-large", bracket_revision=0),
        ]
        db_session = Mock()
        db_session.execute = AsyncMock(return_value=cohort_result)
        db_session.scalar = AsyncMock(side_effect=tournaments)
        db_session.commit = AsyncMock()
        db_session.rollback = AsyncMock()

        advance = AsyncMock(return_value=DeadlockAutomationResult())
        with (
            patch.object(deadlock_automation, "_advance_tournament", advance),
            patch.object(
                deadlock_automation,
                "refresh_tournament_read_models",
                new_callable=AsyncMock,
            ) as refresh_read_models,
        ):
            result = await run_deadlock_automation_tick(
                db_session,
                now=now,
                max_tournaments=3,
            )

        selected_ids = [
            call.kwargs["tournament"].id
            for call in advance.await_args_list
        ]
        self.assertEqual(
            selected_ids,
            ["old-small-a", "old-small-z", "old-large"],
        )
        self.assertNotIn("later-tiny", selected_ids)
        self.assertEqual(result.scanned, 3)
        self.assertEqual(result.deferred, 11)
        self.assertEqual(result.errors, 0)
        self.assertEqual(db_session.commit.await_count, 3)
        self.assertEqual(db_session.rollback.await_count, 0)
        self.assertEqual(refresh_read_models.await_count, 3)

        statement = db_session.execute.await_args.args[0]
        self.assertIn("LIMIT 3", compiled_sql(statement))


if __name__ == "__main__":
    unittest.main()
