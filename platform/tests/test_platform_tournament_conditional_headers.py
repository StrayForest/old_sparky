from __future__ import annotations

import unittest

from starlette.requests import Request
from starlette.responses import Response

from apps.platform_api.app.api.routes import tournaments as tournament_routes


def _request(if_none_match: str | None = None) -> Request:
    headers = []
    if if_none_match is not None:
        headers.append((b"if-none-match", if_none_match.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/example/bracket",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )


class TournamentConditionalHeaderTests(unittest.TestCase):
    def test_weak_cloudflare_validator_matches_strong_origin_etag(self) -> None:
        response = Response()

        result = tournament_routes._conditional_response(
            _request('W/"revision"'),
            response,
            etag='"revision"',
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 304)

    def test_unrelated_validator_does_not_return_not_modified(self) -> None:
        response = Response()

        result = tournament_routes._conditional_response(
            _request('W/"other"'),
            response,
            etag='"revision"',
        )

        self.assertIsNone(result)
        self.assertEqual(response.headers["ETag"], '"revision"')

