from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from apps.platform_api.app.api.routes import tournaments as tournament_routes
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PlatformTournamentWorkspaceHotPathTests(PlatformIsolatedAsyncioTestCase):
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

    def test_single_tournament_count_statement_uses_correlated_counts(self) -> None:
        statement = tournament_routes.tournament_with_counts_stmt()
        sql = str(statement.compile())

        self.assertIn("SELECT count(", sql)
        self.assertNotIn("GROUP BY", sql)

    def test_active_participant_workspace_does_not_load_invite_code(self) -> None:
        tournament = SimpleNamespace(
            organizer_user_id="organizer-1",
            visibility="invite_only",
        )
        active_participant = SimpleNamespace(status="registered")

        self.assertFalse(
            tournament_routes.should_include_workspace_invite_code(
                tournament,
                auth_session=SimpleNamespace(
                    user=SimpleNamespace(id="participant-1"),
                    role_slugs=frozenset(),
                ),
                participant_record=active_participant,
                workspace_visible=True,
            )
        )

    def test_workspace_invite_code_remains_for_public_and_manager_views(self) -> None:
        tournament = SimpleNamespace(
            organizer_user_id="organizer-1",
            visibility="public",
        )

        self.assertTrue(
            tournament_routes.should_include_workspace_invite_code(
                tournament,
                auth_session=None,
                participant_record=None,
                workspace_visible=True,
            )
        )
        self.assertTrue(
            tournament_routes.should_include_workspace_invite_code(
                tournament,
                auth_session=SimpleNamespace(
                    user=SimpleNamespace(id="organizer-1"),
                    role_slugs=frozenset(),
                ),
                participant_record=SimpleNamespace(status="registered"),
                workspace_visible=True,
            )
        )

    def test_workspace_etag_includes_viewer_ready_choice(self) -> None:
        common = {
            "tournament_id": "tournament-1",
            "tournament_state_version": 12,
            "workspace_view": "detail",
            "participants_limit": 0,
            "participants_offset": 0,
            "include_current_user": False,
            "user_id": "user-1",
            "bracket_revision": 3,
            "ready_check_state_version": 77,
            "ready_round_id": 11,
        }

        self.assertNotEqual(
            tournament_routes._workspace_etag_from_values(
                **common,
                ready_check_current_user_choice=None,
            ),
            tournament_routes._workspace_etag_from_values(
                **common,
                ready_check_current_user_choice="yes",
            ),
        )

    def test_workspace_conditional_preflight_statement_is_one_read_shape(self) -> None:
        statement = tournament_routes.workspace_conditional_preflight_stmt()
        sql = str(statement.compile())

        self.assertEqual(sql.upper().count("SELECT"), 8)
        self.assertIn("tournament_deadlock_ready_votes", sql)
        self.assertIn("tournament_deadlock_ready_vote_count_shards", sql)
        self.assertNotIn("JOIN platform.users", sql)

    def test_workspace_base_preflight_statement_includes_viewer_access(self) -> None:
        statement = tournament_routes.workspace_base_preflight_stmt()
        sql = str(statement.compile())

        self.assertIn("workspace_tournament_id", sql)
        self.assertIn("workspace_participant_status", sql)
        self.assertIn("workspace_ready_round_id", sql)
        self.assertNotIn("SELECT platform.tournaments.id, platform.tournaments.slug", sql)
        self.assertIn("JOIN platform.users", sql)
        self.assertIn("LEFT OUTER JOIN platform.player_profiles", sql)
        self.assertNotIn(", platform.users, platform.player_profiles", sql)
        self.assertIn("tournament_participants", sql)
        self.assertIn("player_tournament_commitments", sql)
        self.assertIn("tournament_deadlock_ready_rounds", sql)
        self.assertIn("tournament_deadlock_ready_vote_count_shards", sql)
        self.assertIn("tournament_deadlock_ready_votes", sql)
        self.assertIn("workspace_base_user_id", statement.compile().params)
        self.assertIn("workspace_base_slug", statement.compile().params)

    async def test_authenticated_workspace_reuses_base_preflight_access(self) -> None:
        created_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
        tournament = SimpleNamespace(
            id="tournament-1",
            slug="night-cup",
            visibility="public",
            status="registration_closed",
            format_slug="solo",
            organizer_user_id="organizer-1",
            updated_at=created_at,
            created_at=created_at,
            bracket_revision=0,
        )
        participant = SimpleNamespace(status="registered")
        base_snapshot = tournament_routes.WorkspaceBasePreflight(
            tournament=tournament,
            organizer_display_name="Organizer",
            organizer_avatar_asset_id=None,
            participant_count=500,
            locked_roster_count=0,
            participant_record=participant,
            active_commitment=None,
            ready_check=tournament_routes.WorkspaceReadyCheckPreflight(
                round=None,
                ready_count=0,
                declined_count=0,
                current_user_choice=None,
            ),
        )
        auth_session = SimpleNamespace(
            user=SimpleNamespace(id="user-1"),
            role_slugs=frozenset(),
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/tournaments/night-cup/workspace",
                "headers": [],
                "query_string": b"workspace_view=bracket_summary&include_current_user=false",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 1),
            }
        )

        with (
            patch.object(
                tournament_routes,
                "workspace_base_preflight",
                AsyncMock(return_value=base_snapshot),
            ) as base_lookup,
            patch.object(
                tournament_routes,
                "workspace_access_for_user",
                AsyncMock(side_effect=AssertionError("base preflight owns access read")),
            ),
            patch.object(
                tournament_routes,
                "tournament_media_descriptors",
                AsyncMock(return_value=(None, None)),
            ),
            patch.object(
                tournament_routes,
                "serialize_tournament",
                return_value=tournament_routes.TournamentResponse(
                    id="tournament-1",
                    slug="night-cup",
                    name="Night Cup",
                    description=None,
                    visibility="public",
                    status="registration_closed",
                    format_slug="solo",
                    organizer_user_id="organizer-1",
                    created_at=created_at,
                ),
            ),
            patch.object(
                tournament_routes,
                "can_view_tournament_workspace_data",
                return_value=True,
            ),
            patch.object(
                tournament_routes,
                "build_tournament_workspace_bracket_summary_response",
                return_value=tournament_routes.TournamentBracketResponse(
                    tournament_id="tournament-1",
                    tournament_status="registration_closed",
                    status="pending",
                ),
            ),
            patch.object(tournament_routes, "_workspace_response_etag", return_value='"base"'),
            patch.object(tournament_routes, "_conditional_response", return_value=None),
            patch.object(
                tournament_routes,
                "_serialized_model_response",
                return_value=Response(status_code=200),
            ),
        ):
            result = await tournament_routes.get_tournament_workspace(
                slug="night-cup",
                request=request,
                response=Response(),
                participants_limit=0,
                participants_offset=0,
                workspace_view="bracket_summary",
                include_current_user=False,
                invite_code=None,
                auth_session=auth_session,
                db_session=AsyncMock(),
        )

        self.assertEqual(result.status_code, 200)
        base_lookup.assert_awaited_once()
        self.assertEqual(
            base_lookup.await_args.kwargs,
            {"slug": "night-cup", "user_id": "user-1"},
        )

    async def test_authenticated_workspace_reuses_combined_ready_check_preflight(self) -> None:
        created_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
        tournament = SimpleNamespace(
            id="tournament-1",
            slug="night-cup",
            visibility="public",
            status="registration_closed",
            format_slug="solo",
            organizer_user_id="organizer-1",
            updated_at=created_at,
            created_at=created_at,
            bracket_revision=0,
        )
        ready_round = SimpleNamespace(
            id=11,
            tournament_id="tournament-1",
            status="active",
            eligible_user_ids=["user-1"],
            initiated_by_user_id="organizer-1",
            created_at=created_at,
            closed_at=None,
        )
        base_snapshot = tournament_routes.WorkspaceBasePreflight(
            tournament=tournament,
            organizer_display_name="Organizer",
            organizer_avatar_asset_id=None,
            participant_count=500,
            locked_roster_count=0,
            participant_record=SimpleNamespace(status="registered"),
            active_commitment=None,
            ready_check=tournament_routes.WorkspaceReadyCheckPreflight(
                round=ready_round,
                ready_count=37,
                declined_count=4,
                current_user_choice="yes",
            ),
        )
        auth_session = SimpleNamespace(
            user=SimpleNamespace(id="user-1"),
            role_slugs=frozenset(),
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/tournaments/night-cup/workspace",
                "headers": [],
                "query_string": b"workspace_view=bracket&include_current_user=false",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 1),
            }
        )
        serialized = Mock(return_value=Response(status_code=200))

        with (
            patch.object(
                tournament_routes,
                "workspace_base_preflight",
                AsyncMock(return_value=base_snapshot),
            ),
            patch.object(
                tournament_routes,
                "workspace_access_for_user",
                AsyncMock(side_effect=AssertionError("base preflight owns access read")),
            ),
            patch.object(
                tournament_routes,
                "tournament_media_descriptors",
                AsyncMock(return_value=(None, None)),
            ),
            patch.object(
                tournament_routes,
                "serialize_tournament",
                return_value=tournament_routes.TournamentResponse(
                    id="tournament-1",
                    slug="night-cup",
                    name="Night Cup",
                    description=None,
                    visibility="public",
                    status="registration_closed",
                    format_slug="solo",
                    organizer_user_id="organizer-1",
                    created_at=created_at,
                ),
            ),
            patch.object(
                tournament_routes,
                "can_view_tournament_workspace_data",
                return_value=True,
            ),
            patch.object(
                tournament_routes,
                "build_tournament_bracket_response",
                AsyncMock(
                    return_value=tournament_routes.TournamentBracketResponse(
                        tournament_id="tournament-1",
                        tournament_status="registration_closed",
                        status="pending",
                    )
                ),
            ),
            patch.object(
                tournament_routes,
                "build_deadlock_ready_check_state_response",
                AsyncMock(side_effect=AssertionError("combined preflight owns Ready Check read")),
            ),
            patch.object(tournament_routes, "_workspace_response_etag", return_value='"base"'),
            patch.object(tournament_routes, "_conditional_response", return_value=None),
            patch.object(tournament_routes, "_serialized_model_response", serialized),
        ):
            result = await tournament_routes.get_tournament_workspace(
                slug="night-cup",
                request=request,
                response=Response(),
                participants_limit=0,
                participants_offset=0,
                workspace_view="bracket",
                include_current_user=False,
                invite_code=None,
                auth_session=auth_session,
                db_session=AsyncMock(),
            )

        self.assertEqual(result.status_code, 200)
        workspace_response = serialized.call_args.args[0]
        self.assertEqual(workspace_response.ready_check.active_round.ready_count, 37)
        self.assertEqual(workspace_response.ready_check.active_round.declined_count, 4)
        self.assertEqual(workspace_response.ready_check.active_round.current_user_choice, "yes")
        self.assertEqual(workspace_response.ready_check.latest_round.id, 11)

    async def test_conditional_detail_returns_304_from_one_preflight_query(self) -> None:
        updated_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
        tournament = SimpleNamespace(
            id="tournament-1",
            slug="night-cup",
            visibility="public",
            status="registration_closed",
            format_slug="solo",
            organizer_user_id="organizer-1",
            updated_at=updated_at,
            created_at=updated_at,
            bracket_revision=0,
        )
        db_session = AsyncMock()
        db_session.execute.return_value = Mock(
            first=Mock(
                return_value=(
                    tournament,
                    "registered",
                    500,
                    11,
                    0,
                    0,
                    None,
                )
            )
        )
        auth_session = SimpleNamespace(
            user=SimpleNamespace(id="user-1"),
            role_slugs=frozenset(),
        )
        etag = tournament_routes._workspace_etag_from_values(
            tournament_id="tournament-1",
            tournament_state_version=tournament_routes.tournament_state_version(
                tournament,
                participant_count=500,
            ),
            workspace_view="detail",
            participants_limit=0,
            participants_offset=0,
            include_current_user=False,
            user_id="user-1",
            bracket_revision=0,
            ready_check_state_version=11_000_000,
            ready_round_id=11,
            ready_check_current_user_choice=None,
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/tournaments/night-cup/workspace",
                "headers": [(b"if-none-match", etag.encode("ascii"))],
                "query_string": (
                    b"participants_limit=0&participants_offset=0&"
                    b"workspace_view=detail&include_current_user=false"
                ),
                "scope": {"route": SimpleNamespace(path="/{slug}/workspace")},
            }
        )
        response = Response()

        with (
            patch.object(
                tournament_routes,
                "tournament_media_descriptors",
                AsyncMock(side_effect=AssertionError("304 must skip workspace build")),
            ),
            patch.object(
                tournament_routes,
                "workspace_access_for_user",
                AsyncMock(side_effect=AssertionError("preflight owns access read")),
            ),
            patch.object(
                tournament_routes,
                "build_tournament_workspace_detail_bracket_response",
                AsyncMock(side_effect=AssertionError("304 must skip bracket build")),
            ),
        ):
            result = await tournament_routes.get_tournament_workspace(
                slug="night-cup",
                request=request,
                response=response,
                participants_limit=0,
                participants_offset=0,
                workspace_view="detail",
                include_current_user=False,
                invite_code=None,
                auth_session=auth_session,
                db_session=db_session,
            )

        self.assertEqual(result.status_code, 304)
        self.assertEqual(result.headers["etag"], etag)
        db_session.execute.assert_awaited_once()

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

    async def test_explicit_missing_avatar_does_not_repeat_profile_lookup(self) -> None:
        db_session = AsyncMock()
        tournament = SimpleNamespace(
            organizer_user_id="organizer-1",
            banner_asset_id=None,
        )

        with patch.object(
            tournament_routes,
            "load_media_descriptors",
            AsyncMock(return_value={}),
        ) as load_media:
            cover_media, organizer_avatar_media = await tournament_routes.tournament_media_descriptors(
                db_session,
                tournament,
                organizer_avatar_asset_id=None,
            )

        db_session.scalar.assert_not_awaited()
        load_media.assert_awaited_once_with(db_session, (None, None))
        self.assertIsNone(cover_media)
        self.assertIsNone(organizer_avatar_media)

    async def test_unversioned_ready_state_loads_round_and_counts_in_one_query(self) -> None:
        created_at = datetime.now(timezone.utc)
        round_row = SimpleNamespace(
            id=11,
            tournament_id="tournament-1",
            status="active",
            eligible_user_ids=["user-1"],
            initiated_by_user_id="organizer-1",
            created_at=created_at,
            closed_at=None,
        )
        db_session = AsyncMock()
        db_session.execute.return_value = Mock(
            first=Mock(return_value=(round_row, 1, 0, "yes"))
        )

        state = await tournament_routes.deadlock_ready_state_response_for_tournament(
            db_session,
            tournament_id="tournament-1",
            current_user_id="user-1",
        )

        db_session.execute.assert_awaited_once()
        self.assertEqual(state.active_round.ready_count, 1)
        self.assertEqual(state.active_round.current_user_choice, "yes")
        self.assertEqual(state.latest_round.id, 11)

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
