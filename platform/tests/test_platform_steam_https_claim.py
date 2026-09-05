from __future__ import annotations

import unittest
from datetime import UTC, datetime

import httpx

from apps.platform_api.app.services.steam_openid import (
    OPENID_NS,
    STEAM_OPENID_ENDPOINT,
    verify_openid_assertion,
)
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class SteamHttpsClaimedIdTests(PlatformIsolatedAsyncioTestCase):
    async def test_current_https_claimed_id_is_accepted_and_provider_verified(self) -> None:
        return_to = "https://old-sparky.com/api/v1/auth/steam/callback?state=opaque"
        steam_id = "76561198000000001"
        claimed_id = f"https://steamcommunity.com/openid/id/{steam_id}"
        assertion = {
            "openid.ns": OPENID_NS,
            "openid.mode": "id_res",
            "openid.op_endpoint": STEAM_OPENID_ENDPOINT,
            "openid.claimed_id": claimed_id,
            "openid.identity": claimed_id,
            "openid.return_to": return_to,
            "openid.response_nonce": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "nonce",
            "openid.assoc_handle": "test-association",
            "openid.signed": "signed,op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
            "openid.sig": "provider-signature",
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), STEAM_OPENID_ENDPOINT)
            self.assertIn(b"openid.mode=check_authentication", await request.aread())
            return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")

        settings = PlatformSettings(
            _env_file=None,
            platform_secret_key="steam-https-test-secret",
            platform_auth_flow_ttl_minutes=15,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolved = await verify_openid_assertion(
                assertion,
                return_to,
                settings,
                client,
            )

        self.assertEqual(resolved, steam_id)


if __name__ == "__main__":
    unittest.main()
