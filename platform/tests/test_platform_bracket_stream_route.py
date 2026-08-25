from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.api.routes import tournaments


class PlatformBracketStreamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_route_closes_request_db_session_before_streaming(self) -> None:
        db_session = AsyncMock()
        tournament = SimpleNamespace(id="tournament-1", visibility="public")

        with (
            patch.object(
                tournaments,
                "get_tournament_or_404",
                new=AsyncMock(return_value=tournament),
            ),
            patch.object(
                tournaments,
                "ensure_tournament_workspace_visible",
            ),
            patch.object(
                tournaments,
                "stream_bracket_events",
            ),
        ):
            response = await tournaments.get_tournament_bracket_events(
                "cup-1",
                auth_session=None,
                db_session=db_session,
            )

        self.assertEqual(response.media_type, "text/event-stream")
        db_session.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
