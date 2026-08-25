from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from apps.platform_api.app.api.routes import tournaments
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.tournament_workspace_access import (
    TournamentStreamAccessContext,
)
from python_packages.platform_infra.db import get_db_session


class PlatformBracketStreamRouteTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _route_context(app, path: str):
        return next(
            context
            for included_router in app.router.routes
            if type(included_router).__name__ == "_IncludedRouter"
            for context in included_router.effective_route_contexts()
            if context.path == path
        )

    @staticmethod
    def _db_dependencies(route_context):
        db_dependencies = []

        def collect(dependant) -> None:
            if dependant.call is get_db_session:
                db_dependencies.append(dependant)
            for child in dependant.dependencies:
                collect(child)

        collect(route_context.dependant)
        return db_dependencies

    def test_sse_dependency_graph_uses_function_scoped_db_sessions(self) -> None:
        app = create_app()
        route_context = self._route_context(
            app,
            "/api/v1/tournaments/{slug}/bracket/events",
        )
        db_dependencies = self._db_dependencies(route_context)
        self.assertGreaterEqual(len(db_dependencies), 1)
        self.assertTrue(all(item.scope == "function" for item in db_dependencies))

    def test_regular_tournament_routes_keep_request_scoped_db_sessions(self) -> None:
        app = create_app()
        route_context = self._route_context(app, "/api/v1/tournaments")
        db_dependencies = self._db_dependencies(route_context)
        self.assertGreaterEqual(len(db_dependencies), 1)
        self.assertTrue(any(item.scope is None for item in db_dependencies))

    def test_sse_endpoint_uses_function_scoped_db_dependency(self) -> None:
        parameter = inspect.signature(
            tournaments.get_tournament_bracket_events
        ).parameters["db_session"]
        self.assertEqual(parameter.default.scope, "function")

    async def test_sse_endpoint_reuses_stream_access_participant_snapshot(self) -> None:
        tournament = SimpleNamespace(id="tournament-1")
        auth_session = SimpleNamespace(
            user=SimpleNamespace(id="user-1"),
            role_slugs=frozenset(),
        )
        access_context = TournamentStreamAccessContext(
            decision="active_participant",
            slug="test-tournament",
            user_id="user-1",
            session_id="session-1",
            tournament=tournament,
        )

        async def empty_stream():
            if False:
                yield ""

        with (
            patch.object(
                tournaments,
                "current_tournament_stream_access_context",
                return_value=access_context,
            ),
            patch.object(
                tournaments,
                "ensure_tournament_workspace_visible",
            ) as ensure_visible,
            patch.object(
                tournaments,
                "participant_for_user",
                new_callable=AsyncMock,
            ) as participant_for_user,
            patch.object(
                tournaments,
                "stream_bracket_events",
                return_value=empty_stream(),
            ),
        ):
            response = await tournaments.get_tournament_bracket_events(
                "test-tournament",
                auth_session,
                MagicMock(),
            )

        self.assertEqual(response.media_type, "text/event-stream")
        participant_for_user.assert_not_awaited()
        ensure_visible.assert_called_once_with(
            tournament,
            auth_session=auth_session,
            has_participant_record=True,
        )


if __name__ == "__main__":
    unittest.main()
