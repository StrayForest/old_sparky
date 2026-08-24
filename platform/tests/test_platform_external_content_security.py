from __future__ import annotations

import unittest

import httpx

from apps.platform_api.app.api.routes import content
from apps.platform_api.app.services.external_content_http import (
    BoundedNoRedirectAsyncClient,
)


class PlatformExternalContentSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_refuses_redirect_following(self) -> None:
        requested_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1/internal"},
            )

        async with BoundedNoRedirectAsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            response = await client.get("https://trusted.example/source")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(requested_urls, ["https://trusted.example/source"])

    async def test_client_caps_decoded_response_body(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * 17)
        )
        async with BoundedNoRedirectAsyncClient(
            transport=transport,
            max_response_bytes=16,
        ) as client:
            with self.assertRaisesRegex(ValueError, "byte limit"):
                await client.get("https://trusted.example/source")

    def test_public_home_route_uses_hardened_refresh(self) -> None:
        self.assertEqual(
            content.refresh_home_content.__module__,
            "apps.platform_api.app.services.home_content_security",
        )


if __name__ == "__main__":
    unittest.main()
