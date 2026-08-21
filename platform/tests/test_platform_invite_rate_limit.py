from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request

from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.invite_rate_limit import check_invite_rate_limit


class FakeRedis:
    def __init__(self) -> None:
        self.user_count = 0
        self.ip_count = 0
        self.closed = False

    async def eval(self, _script: str, _count: int, *_args: object) -> list[int]:
        self.user_count += 1
        self.ip_count += 1
        return [self.user_count, self.ip_count]

    async def aclose(self) -> None:
        self.closed = True


def request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/tournaments/invites/claim",
            "headers": [],
            "client": ("192.0.2.10", 12345),
        }
    )


class PlatformInviteRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **overrides: object) -> PlatformSettings:
        values: dict[str, object] = {
            "_env_file": None,
            "platform_secret_key": "unit-test-secret-key-for-invite-limits",
            "platform_invite_rate_limit_enabled": True,
            "platform_invite_claim_user_limit": 2,
            "platform_invite_claim_ip_limit": 3,
        }
        values.update(overrides)
        return PlatformSettings(**values)

    async def test_claim_limit_is_hmac_scoped_and_redis_is_closed(self) -> None:
        fake = FakeRedis()
        with patch(
            "python_packages.platform_infra.invite_rate_limit.redis_client",
            return_value=fake,
        ):
            await check_invite_rate_limit(
                request(), user_id="user-1", operation="claim", settings=self.settings(), now_epoch=10
            )
            await check_invite_rate_limit(
                request(), user_id="user-1", operation="claim", settings=self.settings(), now_epoch=10
            )
            with self.assertRaises(HTTPException) as raised:
                await check_invite_rate_limit(
                    request(),
                    user_id="user-1",
                    operation="claim",
                    settings=self.settings(),
                    now_epoch=10,
                )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.detail["code"], "invite_rate_limited")
        self.assertTrue(fake.closed)

    async def test_production_enables_limits_even_if_flag_is_false(self) -> None:
        fake = FakeRedis()
        with patch(
            "python_packages.platform_infra.invite_rate_limit.redis_client",
            return_value=fake,
        ):
            await check_invite_rate_limit(
                request(),
                user_id="user-1",
                operation="lookup",
                settings=self.settings(
                    platform_environment="production",
                    platform_invite_rate_limit_enabled=False,
                ),
                now_epoch=10,
            )

        self.assertEqual(fake.user_count, 1)


if __name__ == "__main__":
    unittest.main()
