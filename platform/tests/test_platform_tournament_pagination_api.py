from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest

from sqlalchemy.dialects import postgresql
from starlette.responses import Response

from apps.platform_api.app.api.pagination import (
    PARTICIPANT_LIST_DEFAULT_LIMIT,
    PARTICIPANT_LIST_MAX_LIMIT,
    TOURNAMENT_LIST_DEFAULT_LIMIT,
    TOURNAMENT_LIST_MAX_LIMIT,
    encode_cursor,
    set_pagination_headers,
)
from apps.platform_api.app.api.routes.admin import admin_list_tournaments
from apps.platform_api.app.api.routes.tournaments import (
    list_my_tournaments,
    list_tournament_participants,
    list_tournaments,
)
from apps.platform_api.app.api.schemas import TournamentResponse
from apps.platform_api.app.services.tournament_catalog_cache import (
    PublicTournamentListCacheEntry,
)
from python_packages.platform_infra.models import Tournament, TournamentListReadModel


def compiled_sql(statement) -> str:
    return " ".join(
        str(statement.compile(dialect=postgresql.dialect())).split()
    )


def empty_result() -> Mock:
    result = Mock()
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    return result


def tournament_result(row_count: int = 1) -> Mock:
    tournament = TournamentListReadModel(
        id="tournament-1",
        slug="night-cup",
        name="Night Cup",
        description=None,
        cover_url=None,
        visibility="public",
        status="registration_open",
        format_slug="solo",
        allowed_ranks=[],
        max_participants=None,
        registration_starts_at=None,
        registration_closes_at=None,
        ready_check_starts_at=None,
        ready_check_ends_at=None,
        captain_selection_starts_at=None,
        starts_at=None,
        match_format="bo1",
        final_format="bo3",
        captain_response_deadline_minutes=None,
        teams_count=None,
        automation_ready_check_started_at=None,
        automation_ready_check_closed_at=None,
        automation_captain_round_started_at=None,
        automation_captain_round_finalized_at=None,
        automation_assignment_generated_at=None,
        automation_last_error=None,
        organizer_user_id="organizer-1",
        organizer_display_name="Organizer",
        participant_count=7,
        has_locked_deadlock_roster=False,
        bracket_revision=0,
        created_at=datetime(2026, 6, 13, tzinfo=UTC),
        updated_at=datetime(2026, 6, 13, tzinfo=UTC),
    )
    result = Mock()
    result.scalars.return_value.all.return_value = [tournament] * row_count
    result.all.return_value = [(tournament, None)] * row_count
    return result


class PlatformTournamentPaginationTests(unittest.IsolatedAsyncioTestCase):
    def test_pagination_limits_and_headers(self) -> None:
        self.assertEqual(TOURNAMENT_LIST_DEFAULT_LIMIT, 50)
        self.assertEqual(TOURNAMENT_LIST_MAX_LIMIT, 100)
        self.assertEqual(PARTICIPANT_LIST_DEFAULT_LIMIT, 100)
        self.assertEqual(PARTICIPANT_LIST_MAX_LIMIT, 500)

        response = Response()
        set_pagination_headers(
            response,
            total=17,
            limit=9,
            offset=9,
            returned=8,
        )

        self.assertEqual(response.headers["X-Total-Count"], "17")
        self.assertEqual(response.headers["X-Limit"], "9")
        self.assertEqual(response.headers["X-Offset"], "9")
        self.assertEqual(response.headers["X-Has-More"], "false")

    async def test_public_list_filters_and_sorts_before_page_aggregation(self) -> None:
        db_session = Mock()
        db_session.connection = AsyncMock()
        db_session.scalar = AsyncMock()
        db_session.execute = AsyncMock(return_value=tournament_result())
        response = Response()

        payload = await list_tournaments(
            response=response,
            search="Night",
            rank=["Oracle", "Phantom"],
            open_registration=True,
            status_filter=None,
            participants_sort="desc",
            date_sort=None,
            limit=9,
            cursor=None,
            db_session=db_session,
        )

        self.assertEqual(len(payload), 1)
        self.assertIsInstance(payload[0], TournamentResponse)
        self.assertEqual(payload[0].slug, "night-cup")
        self.assertIsNone(payload[0].organizer_avatar_url)
        self.assertEqual(db_session.scalar.await_count, 0)
        self.assertEqual(db_session.execute.await_count, 1)
        self.assertEqual(response.headers["X-Limit"], "9")
        self.assertEqual(response.headers["X-Has-More"], "false")
        self.assertEqual(
            response.headers["Cache-Control"],
            "public, max-age=5, s-maxage=15, stale-while-revalidate=30",
        )
        self.assertNotIn("X-Total-Count", response.headers)
        self.assertNotIn("X-Offset", response.headers)
        self.assertIn(
            "X-Next-Cursor",
            response.headers["Access-Control-Expose-Headers"],
        )

        page_sql = compiled_sql(db_session.execute.await_args.args[0])
        self.assertIn("lower(platform.tournament_list_read_models.name) LIKE", page_sql)
        self.assertIn("lower(platform.tournament_list_read_models.organizer_display_name) LIKE", page_sql)
        self.assertIn("platform.tournament_list_read_models.status =", page_sql)
        self.assertIn("platform.tournament_list_read_models.allowed_ranks ?| ARRAY", page_sql)
        self.assertIn("LIMIT", page_sql)
        self.assertNotIn("OFFSET", page_sql)
        self.assertIn(
            "ORDER BY platform.tournament_list_read_models.participant_count DESC, "
            "platform.tournament_list_read_models.created_at DESC, "
            "platform.tournament_list_read_models.id DESC",
            page_sql,
        )
        self.assertNotIn("COUNT(", page_sql)
        self.assertNotIn("JOIN platform.users", page_sql)

    async def test_public_list_cache_hit_does_not_checkout_database(self) -> None:
        db_session = Mock()
        response = Response()
        cached = PublicTournamentListCacheEntry(
            body=b"[]",
            limit=50,
            has_more=False,
            next_cursor=None,
        )

        with patch(
            "apps.platform_api.app.api.routes.tournaments.get_public_tournament_list_cache",
            AsyncMock(return_value=cached),
        ):
            result = await list_tournaments(
                response=response,
                search=None,
                rank=[],
                open_registration=False,
                status_filter=None,
                participants_sort=None,
                date_sort=None,
                limit=50,
                cursor=None,
                db_session=db_session,
            )

        self.assertEqual(result.status_code, 200)
        db_session.execute.assert_not_called()
        db_session.connection.assert_not_called()

    async def test_public_status_and_date_sort_apply_before_pagination(self) -> None:
        db_session = Mock()
        db_session.connection = AsyncMock()
        db_session.scalar = AsyncMock()
        db_session.execute = AsyncMock(return_value=empty_result())
        response = Response()

        payload = await list_tournaments(
            response=response,
            search=None,
            rank=[],
            open_registration=False,
            status_filter="completed",
            participants_sort=None,
            date_sort="farthest",
            limit=2,
            cursor=None,
            db_session=db_session,
        )

        self.assertEqual(payload, [])
        page_sql = compiled_sql(db_session.execute.await_args.args[0])
        self.assertEqual(db_session.scalar.await_count, 0)
        self.assertIn("platform.tournament_list_read_models.status =", page_sql)
        self.assertIn(
            "ORDER BY platform.tournament_list_read_models.starts_at DESC NULLS LAST, "
            "platform.tournament_list_read_models.created_at DESC, "
            "platform.tournament_list_read_models.id DESC",
            page_sql,
        )

    async def test_mine_uses_one_page_query(self) -> None:
        db_session = Mock()
        db_session.scalar = AsyncMock()
        db_session.execute = AsyncMock(return_value=empty_result())
        response = Response()
        auth_session = SimpleNamespace(
            user=SimpleNamespace(id="user-1"),
            role_slugs=(),
        )

        payload = await list_my_tournaments(
            response=response,
            scope_filter="registered",
            search="Night",
            rank=["Oracle"],
            status_filter="registration_open",
            date_sort="nearest",
            limit=2,
            cursor=None,
            auth_session=auth_session,
            db_session=db_session,
        )

        self.assertEqual(payload, [])
        self.assertEqual(db_session.scalar.await_count, 0)
        self.assertEqual(db_session.execute.await_count, 1)
        db_session.scalars.assert_not_called()
        self.assertEqual(response.headers["X-Has-More"], "false")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertIn(
            "X-Has-More",
            response.headers["Access-Control-Expose-Headers"],
        )

        page_sql = compiled_sql(db_session.execute.await_args.args[0])
        self.assertIn("WITH current_user_participations AS", page_sql)
        self.assertIn("my_tournament_ids AS", page_sql)
        self.assertIn("platform.tournament_participants.status", page_sql)
        self.assertIn("lower(platform.tournament_list_read_models.name) LIKE", page_sql)
        self.assertIn("lower(platform.tournament_list_read_models.organizer_display_name) LIKE", page_sql)
        self.assertIn("platform.tournament_list_read_models.status =", page_sql)
        self.assertIn("platform.tournament_list_read_models.allowed_ranks ?| ARRAY", page_sql)
        self.assertNotIn("OFFSET", page_sql)
        self.assertIn("current_user_participant_status", page_sql)
        self.assertIn(
            "ORDER BY platform.tournament_list_read_models.starts_at ASC NULLS LAST, "
            "platform.tournament_list_read_models.created_at DESC, "
            "platform.tournament_list_read_models.id DESC",
            page_sql,
        )
        self.assertNotIn("SELECT platform.tournament_participants.status", page_sql)
        self.assertNotIn("COUNT(", page_sql)

    async def test_admin_aggregates_only_the_selected_page(self) -> None:
        db_session = Mock()
        db_session.scalar = AsyncMock(return_value=12)
        db_session.execute = AsyncMock(return_value=empty_result())
        response = Response()
        auth_session = SimpleNamespace(role_slugs=("admin",))

        payload = await admin_list_tournaments(
            response=response,
            search=None,
            status_filter=None,
            visibility_filter=None,
            attention=False,
            limit=5,
            offset=5,
            auth_session=auth_session,
            db_session=db_session,
        )

        self.assertEqual(payload, [])
        self.assertEqual(db_session.scalar.await_count, 1)
        self.assertEqual(db_session.execute.await_count, 1)
        self.assertEqual(response.headers["X-Total-Count"], "12")
        self.assertEqual(response.headers["X-Has-More"], "true")

        page_sql = compiled_sql(db_session.execute.await_args.args[0])
        self.assertIn("WITH tournament_page AS", page_sql)
        self.assertIn(
            "platform.tournament_participants.tournament_id IN "
            "(SELECT tournament_page.id FROM tournament_page)",
            page_sql,
        )
        self.assertIn(
            "platform.tournament_deadlock_assignment_runs.tournament_id IN "
            "(SELECT tournament_page.id FROM tournament_page)",
            page_sql,
        )
        self.assertIn(
            "platform.tournament_matches.tournament_id IN "
            "(SELECT tournament_page.id FROM tournament_page)",
            page_sql,
        )
        self.assertIn(
            "ORDER BY tournament_page.created_at DESC, tournament_page.id DESC",
            page_sql,
        )

    async def test_participant_list_is_bounded_and_stably_ordered(self) -> None:
        tournament = Tournament(
            id="tournament-1",
            slug="bounded-cup",
            name="Bounded Cup",
            visibility="public",
            status="registration_open",
            format_slug="solo",
            organizer_user_id="organizer-1",
        )
        db_session = Mock()
        db_session.scalar = AsyncMock(side_effect=[tournament, 520])
        db_session.execute = AsyncMock(return_value=empty_result())
        response = Response()

        payload = await list_tournament_participants(
            slug=tournament.slug,
            response=response,
            limit=500,
            offset=500,
            auth_session=None,
            db_session=db_session,
        )

        self.assertEqual(payload, [])
        self.assertEqual(db_session.scalar.await_count, 2)
        self.assertEqual(db_session.execute.await_count, 1)
        self.assertEqual(response.headers["X-Total-Count"], "520")
        self.assertEqual(response.headers["X-Limit"], "500")
        self.assertEqual(response.headers["X-Offset"], "500")
        self.assertEqual(response.headers["X-Has-More"], "true")

        page_sql = compiled_sql(db_session.execute.await_args.args[0])
        self.assertIn("LIMIT", page_sql)
        self.assertIn("OFFSET", page_sql)
        self.assertIn(
            "ORDER BY platform.tournament_participants.created_at ASC, "
            "platform.tournament_participants.id ASC",
            page_sql,
        )

    async def test_public_cursor_filters_after_the_last_created_tournament(self) -> None:
        db_session = Mock()
        db_session.connection = AsyncMock()
        db_session.execute = AsyncMock(return_value=empty_result())
        response = Response()
        cursor = encode_cursor(
            {
                "v": 1,
                "sort": "created_desc",
                "id": "tournament-1",
                "created_at": "2026-06-13T00:00:00+00:00",
            }
        )

        await list_tournaments(
            response=response,
            search=None,
            rank=[],
            open_registration=False,
            status_filter=None,
            participants_sort=None,
            date_sort=None,
            limit=9,
            cursor=cursor,
            db_session=db_session,
        )

        page_sql = compiled_sql(db_session.execute.await_args.args[0])
        self.assertIn(
            "(platform.tournament_list_read_models.created_at, "
            "platform.tournament_list_read_models.id) <",
            page_sql,
        )
        self.assertNotIn("OFFSET", page_sql)

    async def test_public_list_uses_an_extra_row_to_detect_more(self) -> None:
        db_session = Mock()
        db_session.connection = AsyncMock()
        db_session.scalar = AsyncMock()
        db_session.execute = AsyncMock(return_value=tournament_result(row_count=2))
        response = Response()

        payload = await list_tournaments(
            response=response,
            search=None,
            rank=[],
            open_registration=False,
            status_filter=None,
            participants_sort=None,
            date_sort=None,
            limit=1,
            cursor=None,
            db_session=db_session,
        )

        self.assertEqual(len(payload), 1)
        self.assertEqual(response.headers["X-Has-More"], "true")
        self.assertTrue(response.headers.get("X-Next-Cursor"))
        self.assertEqual(db_session.scalar.await_count, 0)


if __name__ == "__main__":
    unittest.main()
