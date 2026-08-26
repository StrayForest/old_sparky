from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from apps.platform_api.app.api.routes import tournaments
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.tournament_participant_policy import (
    enforce_tournament_participant_policy,
)
from apps.platform_api.app.services.tournament_workspace_access import (
    TournamentStreamAccessContext,
    admit_tournament_bracket_stream,
)
from apps.platform_api.app.services.tournament_write_serialization import (
    serialize_tournament_write_invariants,
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
    def _db_dependencies(route_context, dependency):
        db_dependencies = []

        def collect(dependant) -> None:
            if dependant.call is dependency:
                db_dependencies.append(dependant)
            for child in dependant.dependencies:
                collect(child)

        collect(route_context.dependant)
        return db_dependencies

    def test_sse_route_has_one_conditional_admission_dependency(self) -> None:
        app = create_app()
        route_context = self._route_context(
            app,
            "/api/v1/tournaments/{slug}/bracket/events",
        )
        self.assertEqual(
            len(self._db_dependencies(route_context, get_db_session)),
            0,
        )
        admission_dependencies = []

        def collect(dependant) -> None:
            if dependant.call is admit_tournament_bracket_stream:
                admission_dependencies.append(dependant)
            for child in dependant.dependencies:
                collect(child)

        collect(route_context.dependant)
        self.assertEqual(len(admission_dependencies), 1)

    def test_regular_tournament_routes_keep_request_scoped_db_sessions(self) -> None:
        app = create_app()
        route_context = self._route_context(app, "/api/v1/tournaments")
        db_dependencies = self._db_dependencies(route_context, get_db_session)
        self.assertGreaterEqual(len(db_dependencies), 1)
        self.assertTrue(any(item.scope is None for item in db_dependencies))

    def test_sse_endpoint_has_no_database_dependency(self) -> None:
        self.assertNotIn("db_session", inspect.signature(
            tournaments.get_tournament_bracket_events
        ).parameters)

    def test_sse_router_does_not_include_write_only_policy_dependencies(self) -> None:
        app = create_app()
        route_context = self._route_context(
            app,
            "/api/v1/tournaments/{slug}/bracket/events",
        )
        self.assertEqual(
            self._db_dependencies(route_context, serialize_tournament_write_invariants),
            [],
        )
        self.assertEqual(
            self._db_dependencies(route_context, enforce_tournament_participant_policy),
            [],
        )

    def test_sse_router_uses_conditional_admission_dependency(self) -> None:
        app = create_app()
        route_context = self._route_context(
            app,
            "/api/v1/tournaments/{slug}/bracket/events",
        )
        dependencies = []

        def collect(dependant) -> None:
            if dependant.call is admit_tournament_bracket_stream:
                dependencies.append(dependant)
            for child in dependant.dependencies:
                collect(child)

        collect(route_context.dependant)
        self.assertEqual(len(dependencies), 1)

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
                "stream_bracket_events",
                return_value=empty_stream(),
            ),
        ):
            response = await tournaments.get_tournament_bracket_events("test-tournament")

        self.assertEqual(response.media_type, "text/event-stream")


if __name__ == "__main__":
    unittest.main()
