from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

from apps.platform_api.app.api.routes import tournaments
from apps.platform_api.app.services import tournament_workspace_access
from python_packages.platform_infra.sse_admission import (
    SseAdmissionTicket,
    SseAdmissionTicketInvalid,
    issue_sse_admission_ticket,
    verify_sse_admission_ticket,
)


class PlatformSseAdmissionTicketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            platform_secret_key="test-sse-admission-secret-key-with-32-bytes!!",
        )
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    def test_public_ticket_round_trips_without_session_binding(self) -> None:
        with patch(
            "python_packages.platform_infra.sse_admission.get_settings",
            return_value=self.settings,
        ):
            ticket = issue_sse_admission_ticket(
                tournament_id="tournament-1",
                slug="public-cup",
                access="public",
                now=self.now,
            )
            admitted = verify_sse_admission_ticket(
                ticket,
                expected_slug="public-cup",
                now=self.now + timedelta(seconds=1),
            )

        self.assertEqual(admitted.tournament_id, "tournament-1")
        self.assertEqual(admitted.access, "public")
        self.assertIsNone(admitted.user_id)

    def test_private_ticket_requires_the_same_session_cookie(self) -> None:
        with patch(
            "python_packages.platform_infra.sse_admission.get_settings",
            return_value=self.settings,
        ):
            ticket = issue_sse_admission_ticket(
                tournament_id="tournament-2",
                slug="private-cup",
                access="active_participant",
                user_id="user-1",
                session_id="session-1",
                session_token="session-token-1",
                now=self.now,
            )
            admitted = verify_sse_admission_ticket(
                ticket,
                expected_slug="private-cup",
                session_token="session-token-1",
                now=self.now + timedelta(seconds=1),
            )
            with self.assertRaises(SseAdmissionTicketInvalid):
                verify_sse_admission_ticket(
                    ticket,
                    expected_slug="private-cup",
                    session_token="session-token-2",
                    now=self.now + timedelta(seconds=1),
                )

        self.assertEqual(admitted.user_id, "user-1")
        self.assertEqual(admitted.session_id, "session-1")

    def test_ticket_rejects_tampering_wrong_slug_and_expiry(self) -> None:
        with patch(
            "python_packages.platform_infra.sse_admission.get_settings",
            return_value=self.settings,
        ):
            ticket = issue_sse_admission_ticket(
                tournament_id="tournament-3",
                slug="public-cup",
                access="public",
                now=self.now,
            )
            with self.assertRaises(SseAdmissionTicketInvalid):
                verify_sse_admission_ticket(
                    ticket[:-1] + ("a" if ticket[-1] != "a" else "b"),
                    expected_slug="public-cup",
                    now=self.now + timedelta(seconds=1),
                )
            with self.assertRaises(SseAdmissionTicketInvalid):
                verify_sse_admission_ticket(
                    ticket,
                    expected_slug="other-cup",
                    now=self.now + timedelta(seconds=1),
                )
            with self.assertRaises(SseAdmissionTicketInvalid):
                verify_sse_admission_ticket(
                    ticket,
                    expected_slug="public-cup",
                    now=self.now + timedelta(seconds=301),
                )
            with self.assertRaises(SseAdmissionTicketInvalid):
                verify_sse_admission_ticket(
                    "é.invalid",
                    expected_slug="public-cup",
                    now=self.now,
                )

    def test_public_route_ticket_is_not_bound_to_authenticated_session(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/tournaments/public-cup/workspace",
                "headers": [(b"cookie", b"deadlock_platform_session=session-token")],
                "query_string": b"",
                "client": ("127.0.0.1", 1000),
                "server": ("127.0.0.1", 8010),
                "scheme": "http",
            }
        )
        auth_session = SimpleNamespace(
            user=SimpleNamespace(id="user-1"),
            session=SimpleNamespace(id="session-1"),
        )
        tournament = SimpleNamespace(
            id="tournament-1",
            slug="public-cup",
            visibility="public",
            organizer_user_id="organizer-1",
        )
        with (
            patch.object(
                tournaments,
                "get_settings",
                return_value=SimpleNamespace(
                    platform_session_cookie_name="deadlock_platform_session"
                ),
            ),
            patch.object(
                tournaments,
                "issue_sse_admission_ticket",
                return_value="ticket",
            ) as issue_ticket,
        ):
            result = tournaments._issue_tournament_sse_admission_ticket(
                request,
                tournament=tournament,
                auth_session=auth_session,
                has_participant_record=False,
            )

        self.assertEqual(result, "ticket")
        issue_ticket.assert_called_once_with(
            tournament_id="tournament-1",
            slug="public-cup",
            access="public",
            user_id=None,
            session_id=None,
            session_token=None,
        )


class PlatformSseTicketRouteAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            platform_secret_key="test-sse-admission-secret-key-with-32-bytes!!",
        )
        self.now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    async def test_valid_public_ticket_skips_stream_db_admission(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/tournaments/public-cup/bracket/events",
                "path_params": {"slug": "public-cup"},
                "headers": [],
                "query_string": b"ticket=short-lived",
                "client": ("127.0.0.1", 1000),
                "server": ("127.0.0.1", 8010),
                "scheme": "http",
            }
        )
        admitted = SseAdmissionTicket(
            tournament_id="tournament-1",
            slug="public-cup",
            access="public",
            issued_at=1,
            expires_at=2,
        )
        with (
            patch.object(
                tournament_workspace_access,
                "verify_sse_admission_ticket",
                return_value=admitted,
            ),
            patch.object(
                tournament_workspace_access,
                "stream_db_session",
                MagicMock(),
            ) as stream_db_session,
        ):
            await tournament_workspace_access.admit_tournament_bracket_stream(
                request,
                "public-cup",
                ticket="short-lived",
            )

        stream_db_session.assert_not_called()
        context = tournament_workspace_access.current_tournament_stream_access_context()
        self.assertIsNotNone(context)
        self.assertEqual(context.tournament_id, "tournament-1")
        self.assertEqual(context.decision, "public")

    async def test_private_ticket_adds_the_authenticated_user_lease(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/tournaments/private-cup/bracket/events",
                "path_params": {"slug": "private-cup"},
                "headers": [(b"cookie", b"deadlock_platform_session=session-token")],
                "query_string": b"ticket=short-lived",
                "client": ("127.0.0.1", 1000),
                "server": ("127.0.0.1", 8010),
                "scheme": "http",
            }
        )
        admitted = SseAdmissionTicket(
            tournament_id="tournament-2",
            slug="private-cup",
            access="active_participant",
            issued_at=1,
            expires_at=2,
            user_id="user-1",
            session_id="session-1",
            session_digest="digest",
        )
        add_user_scope = AsyncMock()
        with (
            patch.object(
                tournament_workspace_access,
                "get_settings",
                return_value=MagicMock(platform_session_cookie_name="deadlock_platform_session"),
            ),
            patch.object(
                tournament_workspace_access,
                "verify_sse_admission_ticket",
                return_value=admitted,
            ),
            patch.object(
                tournament_workspace_access,
                "add_sse_authenticated_user_scope",
                add_user_scope,
            ),
        ):
            await tournament_workspace_access.admit_tournament_bracket_stream(
                request,
                "private-cup",
                ticket="short-lived",
            )

        add_user_scope.assert_awaited_once_with(request, "user-1")
