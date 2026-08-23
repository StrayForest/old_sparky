from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_api.app.main import create_app
from apps.platform_api.app.services import tournament_workflow as workflow
from python_packages.platform_domain.tournaments import TournamentWorkflowError


class PlatformBackendAuditRemediationTests(unittest.IsolatedAsyncioTestCase):
    async def test_published_assignment_is_superseded_before_replacement(self) -> None:
        published = SimpleNamespace(id="old-run", status="published")
        with patch.object(
            workflow,
            "deadlock_published_auto_assignment_run_for_tournament",
            AsyncMock(return_value=published),
        ):
            result = await workflow.supersede_published_deadlock_assignment_run_for_tournament(
                Mock(),
                tournament_id="tournament",
                replacement_run_id="new-run",
            )
        self.assertIs(result, published)
        self.assertEqual(published.status, "superseded")

    async def test_locked_assignment_cannot_be_superseded(self) -> None:
        locked = SimpleNamespace(id="locked-run", status="locked")
        with patch.object(
            workflow,
            "deadlock_published_auto_assignment_run_for_tournament",
            AsyncMock(return_value=locked),
        ):
            with self.assertRaises(TournamentWorkflowError):
                await workflow.supersede_published_deadlock_assignment_run_for_tournament(
                    Mock(),
                    tournament_id="tournament",
                    replacement_run_id="new-run",
                )

    async def test_closed_ready_round_exclusion_removes_vote_and_eligibility(self) -> None:
        closed_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
        round_row = SimpleNamespace(
            id=77,
            status="closed",
            eligible_user_ids=["keep", "remove"],
            closed_at=closed_at,
        )
        vote = SimpleNamespace(user_id="remove", choice="yes")
        scalar_result = SimpleNamespace(all=lambda: [vote])
        db_session = Mock()
        db_session.scalars = AsyncMock(return_value=scalar_result)
        db_session.execute = AsyncMock()
        tournament = SimpleNamespace(id="tournament", slug="audit-remediation")
        with (
            patch.object(
                workflow,
                "deadlock_ready_state_round_for_tournament",
                AsyncMock(return_value=(None, round_row)),
            ),
            patch.object(workflow, "write_audit_log", AsyncMock()),
        ):
            result = await workflow.prune_participant_from_active_ready_round(
                db_session,
                tournament=tournament,
                user_id="remove",
                actor_user_id="organizer",
                now=datetime(2026, 8, 23, 12, 5, tzinfo=UTC),
                participant_status="disqualified",
            )
        self.assertIs(result, round_row)
        self.assertEqual(round_row.status, "closed")
        self.assertEqual(round_row.closed_at, closed_at)
        self.assertEqual(round_row.eligible_user_ids, ["keep"])
        db_session.execute.assert_awaited_once()

    def test_retired_captain_endpoints_are_hidden_from_openapi(self) -> None:
        schema = create_app().openapi()
        paths = schema["paths"]
        self.assertNotIn(
            "/api/v1/tournaments/{slug}/deadlock/captain-round/respond",
            paths,
        )
        self.assertNotIn(
            "/api/v1/tournaments/{slug}/deadlock/captain-round/close",
            paths,
        )
        self.assertNotIn(
            "/api/v1/tournaments/{slug}/deadlock/captain-round/finalize",
            paths,
        )


if __name__ == "__main__":
    unittest.main()
