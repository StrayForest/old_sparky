from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.platform_api.app.services import tournament_workspace_access as access


class _ExecuteResult:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row) -> None:
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    async def execute(self, _statement):
        return _ExecuteResult(self._row)


class _FakePublicSession:
    def __init__(self, visibility: str) -> None:
        self.visibility = visibility
        self.scalar_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    async def scalar(self, _statement):
        self.scalar_calls += 1
        return self.visibility


class PlatformSseSessionRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_stream_revalidation_checks_visibility_without_session_sql(self) -> None:
        context = access.TournamentStreamAccessContext(
            decision="public",
            slug="public-cup",
            user_id="user-1",
            session_id="session-1",
        )
        token = access._tournament_stream_access_context.set(context)
        db_session = _FakePublicSession("public")
        session_check = AsyncMock(return_value=False)
        try:
            with (
                patch.object(
                    access,
                    "stream_db_session",
                    MagicMock(return_value=db_session),
                ),
                patch.object(
                    access,
                    "_authenticated_stream_session_is_current",
                    session_check,
                ),
            ):
                self.assertTrue(
                    await access.current_tournament_stream_access_is_valid(
                        "tournament-1"
                    )
                )
        finally:
            access._tournament_stream_access_context.reset(token)

        self.assertEqual(db_session.scalar_calls, 1)
        session_check.assert_not_awaited()

    async def test_private_organizer_stream_stops_when_session_is_revoked(self) -> None:
        context = access.TournamentStreamAccessContext(
            decision="organizer",
            slug="private-cup",
            user_id="organizer-1",
            session_id="session-1",
        )
        token = access._tournament_stream_access_context.set(context)
        session_check = AsyncMock(return_value=False)
        try:
            with (
                patch.object(
                    access,
                    "stream_db_session",
                    MagicMock(return_value=_FakeSession(("invite_only", "organizer-1", None))),
                ),
                patch.object(
                    access,
                    "_authenticated_stream_session_is_current",
                    session_check,
                ),
            ):
                self.assertFalse(
                    await access.current_tournament_stream_access_is_valid(
                        "tournament-1"
                    )
                )
        finally:
            access._tournament_stream_access_context.reset(token)

        session_check.assert_awaited_once()
        self.assertEqual(session_check.await_args.kwargs["user_id"], "organizer-1")
        self.assertEqual(session_check.await_args.kwargs["session_id"], "session-1")

    async def test_private_admin_stream_stops_when_role_is_removed(self) -> None:
        context = access.TournamentStreamAccessContext(
            decision="admin",
            slug="private-cup",
            user_id="admin-1",
            session_id="session-2",
        )
        token = access._tournament_stream_access_context.set(context)
        session_check = AsyncMock(return_value=True)
        role_check = AsyncMock(return_value=False)
        try:
            with (
                patch.object(
                    access,
                    "stream_db_session",
                    MagicMock(return_value=_FakeSession(("invite_only", "organizer-1", None))),
                ),
                patch.object(
                    access,
                    "_authenticated_stream_session_is_current",
                    session_check,
                ),
                patch.object(access, "_user_still_has_admin_role", role_check),
            ):
                self.assertFalse(
                    await access.current_tournament_stream_access_is_valid(
                        "tournament-1"
                    )
                )
        finally:
            access._tournament_stream_access_context.reset(token)

        session_check.assert_awaited_once()
        role_check.assert_awaited_once()

    async def test_active_participant_still_requires_current_session(self) -> None:
        context = access.TournamentStreamAccessContext(
            decision="active_participant",
            slug="private-cup",
            user_id="player-1",
            session_id="session-3",
        )
        token = access._tournament_stream_access_context.set(context)
        session_check = AsyncMock(return_value=False)
        try:
            with (
                patch.object(
                    access,
                    "stream_db_session",
                    MagicMock(return_value=_FakeSession(("invite_only", "organizer-1", "registered"))),
                ),
                patch.object(
                    access,
                    "_authenticated_stream_session_is_current",
                    session_check,
                ),
            ):
                self.assertFalse(
                    await access.current_tournament_stream_access_is_valid(
                        "tournament-1"
                    )
                )
        finally:
            access._tournament_stream_access_context.reset(token)

        session_check.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
