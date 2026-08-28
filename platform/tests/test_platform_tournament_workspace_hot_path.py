from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from apps.platform_api.app.api.routes import tournaments as tournament_routes


class PlatformTournamentWorkspaceHotPathTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        tournament_routes._public_workspace_snapshot_cache.clear()

    def test_serialized_model_response_uses_json_payload_and_etag(self) -> None:
        class Payload(BaseModel):
            value: int

        response = tournament_routes._serialized_model_response(
            Payload(value=7),
            etag='"revision"',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["etag"], '"revision"')
        self.assertEqual(response.body, b'{"value":7}')

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

    async def test_public_workspace_snapshot_serves_without_rebuilding_workspace(self) -> None:
        created_at = datetime.now(timezone.utc)
        tournament_response = tournament_routes.TournamentResponse(
            id="tournament-1",
            slug="public-cup",
            name="Public Cup",
            description=None,
            visibility="public",
            status="registration_open",
            format_slug="solo",
            organizer_user_id="organizer-1",
            created_at=created_at,
        )
        snapshot_response = tournament_routes.TournamentWorkspaceResponse(
            tournament=tournament_response,
            server_time=created_at,
        )
        tournament_routes._set_public_workspace_snapshot_cache(
            "public-cup",
            tournament_id="tournament-1",
            tournament_updated_at=created_at,
            response=snapshot_response,
        )
        current_tournament = SimpleNamespace(
            id="tournament-1",
            slug="public-cup",
            visibility="public",
            status="registration_open",
            organizer_user_id="organizer-1",
            updated_at=created_at,
        )
        db_session = AsyncMock()
        db_session.scalar = AsyncMock(return_value=current_tournament)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/tournaments/public-cup/workspace",
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 1),
            }
        )

        server_time_before = datetime.now(timezone.utc)
        response = await tournament_routes.get_tournament_workspace(
            "public-cup",
            request,
            Response(),
            participants_limit=0,
            participants_offset=0,
            workspace_view="detail",
            include_current_user=False,
            auth_session=None,
            db_session=db_session,
        )
        server_time_after = datetime.now(timezone.utc)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["tournament"]["participant_count"], 0)
        server_time = datetime.fromisoformat(payload["server_time"].replace("Z", "+00:00"))
        self.assertGreaterEqual(server_time, server_time_before)
        self.assertLessEqual(server_time, server_time_after)
        db_session.scalar.assert_awaited_once()
        db_session.execute.assert_not_awaited()

    def test_runtime_invalidation_removes_public_snapshot(self) -> None:
        snapshot_response = tournament_routes.TournamentWorkspaceResponse.model_construct()
        tournament_routes._set_public_workspace_snapshot_cache(
            "public-cup",
            tournament_id="tournament-1",
            tournament_updated_at=None,
            response=snapshot_response,
        )

        tournament_routes.invalidate_tournament_runtime_caches("tournament-1")

        self.assertIsNone(
            tournament_routes._get_public_workspace_snapshot_cache("public-cup")
        )


if __name__ == "__main__":
    unittest.main()
