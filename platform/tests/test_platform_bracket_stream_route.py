from __future__ import annotations

import inspect
import unittest

from apps.platform_api.app.api.routes import tournaments
from apps.platform_api.app.main import create_app
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


if __name__ == "__main__":
    unittest.main()
