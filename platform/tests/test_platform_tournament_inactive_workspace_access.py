from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from apps.platform_api.app.services.tournament_workspace_access import (
    ACTIVE_PARTICIPANT_STATUSES,
    enforce_tournament_bearer_read_rate_limit,
    ensure_private_tournament_read_membership_is_active,
    normalize_bearer_invite_code,
    private_tournament_bearer_route_slug_from_request,
    private_tournament_child_slug_from_request,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class _ExecuteResult:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


def _request(
    path: str,
    *,
    method: str = "GET",
    route_path: str | None = None,
    path_params: dict[str, str] | None = None,
    query_string: str = "",
) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
        "path_params": dict(path_params or {}),
    }
    if route_path is not None:
        scope["route"] = SimpleNamespace(path=route_path)
    return Request(scope)


def _tournament_child_request(
    slug: str,
    suffix: str,
    *,
    method: str = "GET",
    route_suffix: str | None = None,
    query_string: str = "",
) -> Request:
    return _request(
        f"/api/v1/tournaments/{slug}/{suffix}",
        method=method,
        route_path=f"/api/v1/tournaments/{{slug}}/{route_suffix or suffix}",
        path_params={"slug": slug},
        query_string=query_string,
    )


def _auth_session(user_id: str = "user-1", *roles: str):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        role_slugs=frozenset(roles),
    )


def _db_session(row):
    return SimpleNamespace(
        execute=AsyncMock(return_value=_ExecuteResult(row)),
        scalar=AsyncMock(return_value=None if row is None else row[0]),
    )


class PlatformTournamentInactiveWorkspaceAccessTests(PlatformIsolatedAsyncioTestCase):
    def test_bearer_route_allowlist_is_closed_and_template_exact(self) -> None:
        allowed_suffixes = ("", "workspace", "participants", "matches", "bracket")
        for suffix in allowed_suffixes:
            with self.subTest(suffix=suffix):
                request = _request(
                    f"/api/v1/tournaments/private-cup/{suffix}" if suffix else "/api/v1/tournaments/private-cup",
                    route_path=(
                        f"/api/v1/tournaments/{{slug}}/{suffix}" if suffix else "/api/v1/tournaments/{slug}"
                    ),
                    path_params={"slug": "private-cup"},
                )
                self.assertEqual(
                    private_tournament_bearer_route_slug_from_request(request),
                    "private-cup",
                )

        for route_path in (
            "/api/v1/tournaments/{slug}/future-private-surface",
            "/api/v1/tournaments/{slug}/workspace/extra",
            "/api/v1/tournaments/{slug}/profiles/{user_id}",
        ):
            with self.subTest(route_path=route_path):
                self.assertIsNone(
                    private_tournament_bearer_route_slug_from_request(
                        _request(
                            "/api/v1/tournaments/private-cup/future",
                            route_path=route_path,
                            path_params={"slug": "private-cup"},
                        )
                    )
                )

        # The helper trusts FastAPI's parsed parameter only after the exact
        # matched template is confirmed; it never derives a slug from URL text.
        self.assertEqual(
            private_tournament_bearer_route_slug_from_request(
                _request(
                    "/api/v1/tournaments/other-cup/workspace",
                    route_path="/api/v1/tournaments/{slug}/workspace",
                    path_params={"slug": "private-cup"},
                )
            ),
            "private-cup",
        )

    async def test_bearer_query_parser_rejects_duplicate_and_lossy_values(self) -> None:
        for query_string in ("invite_code=ABCDEFGHIJ&invite_code=KLMNOPQRST", "invite_code=ABCDEFGHI-"):
            with self.subTest(query_string=query_string):
                with self.assertRaises(HTTPException) as raised:
                    await ensure_private_tournament_read_membership_is_active(
                        _tournament_child_request(
                            "private-cup",
                            "workspace",
                            query_string=query_string,
                        ),
                        auth_session=None,
                        db_session=_db_session(None),
                    )
                self.assertEqual(raised.exception.status_code, 422)

    def test_bearer_code_normalization_is_strict_and_case_insensitive(self) -> None:
        self.assertEqual(normalize_bearer_invite_code("abcdefghij"), "ABCDEFGHIJ")
        for malformed in (None, "", "ABCDEFGHI", "ABCDEFGHIJKLMNOPQRSTUVWXY", "ABCDEFGHI-", "ABCDEFGHI J", "АБВГДЕЖЗИЙ"):
            with self.subTest(value=malformed):
                self.assertIsNone(normalize_bearer_invite_code(malformed))

    async def test_bearer_rate_limit_is_called_only_for_opted_in_presented_code(self) -> None:
        request = _tournament_child_request(
            "private-cup",
            "workspace",
            query_string="invite_code=ABCDEFGHIJ",
        )
        with patch(
            "apps.platform_api.app.services.tournament_workspace_access.check_invite_rate_limit",
            new_callable=AsyncMock,
        ) as limiter:
            await enforce_tournament_bearer_read_rate_limit(request, auth_session=None)
            limiter.assert_awaited_once()
            self.assertEqual(limiter.await_args.kwargs["operation"], "bearer_read")
            self.assertEqual(limiter.await_args.kwargs["code"], "ABCDEFGHIJ")

            limiter.reset_mock()
            await enforce_tournament_bearer_read_rate_limit(
                _tournament_child_request(
                    "private-cup",
                    "future-private-surface",
                    query_string="invite_code=ABCDEFGHIJ",
                ),
                auth_session=None,
            )
            limiter.assert_not_awaited()

            await enforce_tournament_bearer_read_rate_limit(
                _tournament_child_request("private-cup", "workspace"),
                auth_session=None,
            )
            limiter.assert_not_awaited()

    def test_child_route_detection_uses_matched_route_not_suffix_allowlist(self) -> None:
        route_cases = (
            ("workspace", None),
            ("participants", None),
            ("matches", None),
            ("bracket", None),
            ("deadlock/ready-check", None),
            ("profiles/user-2", "profiles/{user_id}"),
            ("future-private-surface", None),
        )
        for suffix, route_suffix in route_cases:
            with self.subTest(suffix=suffix):
                request = _tournament_child_request(
                    "private-cup",
                    suffix,
                    route_suffix=route_suffix,
                )
                self.assertEqual(
                    private_tournament_child_slug_from_request(request),
                    "private-cup",
                )

        summary_request = _request(
            "/api/v1/tournaments/private-cup",
            route_path="/api/v1/tournaments/{slug}",
            path_params={"slug": "private-cup"},
        )
        self.assertIsNone(private_tournament_child_slug_from_request(summary_request))

        collection_request = _request(
            "/api/v1/tournaments/invites/suggest-code",
            route_path="/api/v1/tournaments/invites/suggest-code",
        )
        self.assertIsNone(private_tournament_child_slug_from_request(collection_request))

        self.assertIsNone(
            private_tournament_child_slug_from_request(
                _tournament_child_request(
                    "private-cup",
                    "workspace",
                    method="POST",
                )
            )
        )

    async def test_private_inactive_or_unclassified_participant_is_denied(self) -> None:
        denied_statuses = ("withdrawn", "disqualified", "suspended")
        for participant_status in denied_statuses:
            with self.subTest(status=participant_status):
                with self.assertRaises(HTTPException) as raised:
                    await ensure_private_tournament_read_membership_is_active(
                        _tournament_child_request("private-cup", "workspace"),
                        auth_session=_auth_session(),
                        db_session=_db_session(
                            ("invite_only", "organizer-1", participant_status)
                        ),
                    )
                self.assertEqual(raised.exception.status_code, 403)
                self.assertIn("Inactive tournament participants", raised.exception.detail)

    async def test_guard_preserves_existing_access_for_active_and_non_member_users(self) -> None:
        self.assertEqual(
            ACTIVE_PARTICIPANT_STATUSES,
            frozenset({"registered", "confirmed", "checked_in"}),
        )
        allowed_rows = (
            ("invite_only", "organizer-1", "registered"),
            ("invite_only", "organizer-1", "confirmed"),
            ("invite_only", "organizer-1", "checked_in"),
            ("invite_only", "organizer-1", None),
            ("public", "organizer-1", "withdrawn"),
        )
        for row in allowed_rows:
            with self.subTest(row=row):
                await ensure_private_tournament_read_membership_is_active(
                    _tournament_child_request("private-cup", "workspace"),
                    auth_session=_auth_session(),
                    db_session=_db_session(row),
                )

    async def test_anonymous_unknown_tournament_is_not_confused_with_private_access(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await ensure_private_tournament_read_membership_is_active(
                _tournament_child_request("missing-cup", "workspace"),
                auth_session=None,
                db_session=_db_session(None),
            )
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(raised.exception.detail, "Tournament not found.")

    async def test_organizer_and_platform_admin_keep_management_access(self) -> None:
        inactive_private_row = ("invite_only", "user-1", "disqualified")
        organizer_db = _db_session(inactive_private_row)
        await ensure_private_tournament_read_membership_is_active(
            _tournament_child_request("private-cup", "bracket"),
            auth_session=_auth_session(),
            db_session=organizer_db,
        )

        admin_db = _db_session(("invite_only", "organizer-1", "disqualified"))
        await ensure_private_tournament_read_membership_is_active(
            _tournament_child_request("private-cup", "bracket"),
            auth_session=_auth_session("user-1", "admin"),
            db_session=admin_db,
        )
        admin_db.execute.assert_not_awaited()

    async def test_summary_and_collection_routes_do_not_add_database_work(self) -> None:
        for request in (
            _request(
                "/api/v1/tournaments/private-cup",
                route_path="/api/v1/tournaments/{slug}",
                path_params={"slug": "private-cup"},
            ),
            _request(
                "/api/v1/tournaments/invites/suggest-code",
                route_path="/api/v1/tournaments/invites/suggest-code",
            ),
        ):
            with self.subTest(path=request.url.path):
                db_session = _db_session(None)
                await ensure_private_tournament_read_membership_is_active(
                    request,
                    auth_session=_auth_session(),
                    db_session=db_session,
                )
                db_session.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
