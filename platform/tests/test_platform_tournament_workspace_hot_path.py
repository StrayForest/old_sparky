from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.api.routes import tournaments as tournament_routes


class PlatformTournamentWorkspaceHotPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_open_detail_does_not_query_assignment_state(self) -> None:
        published_lookup = AsyncMock(return_value=None)
        tournament = SimpleNamespace(
            id="tournament-1",
            status="registration_open",
            format_slug="solo",
            bracket_revision=0,
        )

        with patch.object(
            tournament_routes,
            "deadlock_published_auto_assignment_run_for_tournament",
            published_lookup,
        ):
            response = await tournament_routes.build_tournament_workspace_detail_bracket_response(
                AsyncMock(),
                tournament=tournament,
                can_manage=False,
            )

        published_lookup.assert_not_awaited()
        self.assertEqual(response.status, "pending")
        self.assertEqual(response.teams, [])
        self.assertEqual(response.matches, [])


if __name__ == "__main__":
    unittest.main()
