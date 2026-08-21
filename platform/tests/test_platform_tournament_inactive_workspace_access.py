from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request

from apps.platform_api.app.api.router import api_router
from apps.platform_api.app.services.tournament_workspace_access import (
    PRIVATE_WORKSPACE_READ_SUFFIXES,
    ensure_inactive_participant_has_no_private_workspace_access,
    private_workspace_slug_from_request,
)


class _ExecuteResult:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


def _request(path: str, *, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        }
    )


def _auth_session(user_id: str = "user-1", *roles: str):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        role_slugs=frozenset(roles),
    )


def _db_session(row):
    return SimpleNamespace(execute=AsyncMock(return_value=_ExecuteResult(row)))


class PlatformTournamentInactiveWorkspaceAccessTests(unittest.IsolatedAsyncioTestCase):
    def test_private_workspace_path_detection_is_narrow(self) -> None:
        for suffix in PRIVATE_WORKSPACE_READ_SUFFIXES:
            with self.subTest(suffix=suffix):
                request = _request(f"/api/v1/tournaments/private-cup/{suffix}")
                self.assertEqual(private_workspace_slug_from_request(request), "private-cup")

        self.assertIsNone(
            private_workspace_slug_from_request(_request("/api/v1/tournaments/private-cup"))
        )
        self.assertIsNone(
            private_workspace_slug_from_request(
                _request("/api/v1/tournaments/private-cup/invites")
            )
        )
        self.assertIsNone(
            private_workspace_slug_from_request(
                _request("/api/v1/tournaments/private-cup/workspace", method="POST")
            )
        )

    async def test_private_inactive_participant_is_denied_for_every_workspace_read(self) -> None:
        for participant_status in ("withdrawn", "disqualified"):
            for suffix in PRIVATE_WORKSPACE_READ_SUFFIXES:
                with self.subTest(status=participant_status, suffix=suffix):
                    with self.assertRaises(HTTPException) as raised:
                        await ensure_inactive_participant_has_no_private_workspace_access(
                            _request(f"/api/v1/tournaments/private-cup/{suffix}"),
                            auth_session=_auth_session(),
                            db_session=_db_session(
                                ("invite_only", "organizer-1", participant_status)
                            ),
                        )
                    self.assertEqual(raised.exception.status_code, 403)
                    self.assertIn("Inactive tournament participants", raised.exception.detail)

    async def test_guard_preserves_existing_access_for_active_and_non_member_users(self) -> None:
        allowed_rows = (
            ("invite_only", "organizer-1", "registered"),
            ("invite_only", "organizer-1", "confirmed"),
            ("invite_only", "organizer-1", "checked_in"),
            ("invite_only", "organizer-1", None),
            ("public", "organizer-1", "withdrawn"),
        )
        for row in allowed_rows:
            with self.subTest(row=row):
                await ensure_inactive_participant_has_no_private_workspace_access(
                    _request("/api/v1/tournaments/private-cup/workspace"),
                    auth_session=_auth_session(),
                    db_session=_db_session(row),
                )

    async def test_organizer_and_platform_admin_keep_management_access(self) -> None:
        inactive_private_row = ("invite_only", "user-1", "disqualified")
        organizer_db = _db_session(inactive_private_row)
        await ensure_inactive_participant_has_no_private_workspace_access(
            _request("/api/v1/tournaments/private-cup/bracket"),
            auth_session=_auth_session(),
            db_session=organizer_db,
        )

        admin_db = _db_session(("invite_only", "organizer-1", "disqualified"))
        await ensure_inactive_participant_has_no_private_workspace_access(
            _request("/api/v1/tournaments/private-cup/bracket"),
            auth_session=_auth_session("user-1", "admin"),
            db_session=admin_db,
        )
        admin_db.execute.assert_not_awaited()

    async def test_unrelated_tournament_route_does_not_add_database_work(self) -> None:
        db_session = _db_session(None)
        await ensure_inactive_participant_has_no_private_workspace_access(
            _request("/api/v1/tournaments/private-cup"),
            auth_session=_auth_session(),
            db_session=db_session,
        )
        db_session.execute.assert_not_awaited()

    def test_tournament_router_wires_guard_as_a_top_level_dependency(self) -> None:
        guarded_paths = {
            "/tournaments/{slug}/workspace",
            "/tournaments/{slug}/participants",
            "/tournaments/{slug}/matches",
            "/tournaments/{slug}/bracket",
            "/tournaments/{slug}/bracket/events",
        }
        discovered: set[str] = set()
        for route in api_router.routes:
            if not isinstance(route, APIRoute) or route.path not in guarded_paths:
                continue
            discovered.add(route.path)
            dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(
                ensure_inactive_participant_has_no_private_workspace_access,
                dependency_calls,
                route.path,
            )
        self.assertEqual(discovered, guarded_paths)


if __name__ == "__main__":
    unittest.main()
