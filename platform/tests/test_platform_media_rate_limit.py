from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request

from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.media_rate_limit import check_media_upload_rate_limit


class FakeRedis:
    def __init__(self) -> None:
        self.user_count = 0
        self.ip_count = 0
        self.user_bytes = 0
        self.closed = False

    async def eval(self, _script: str, count: int, *_args: object) -> list[int]:
        self.user_count += 1
        if count == 2:
            self.user_bytes += int(_args[-1])
            return [self.user_count, self.user_bytes]
        self.ip_count += 1
        self.user_bytes += int(_args[-1])
        return [self.user_count, self.ip_count, self.user_bytes]

    async def aclose(self) -> None:
        self.closed = True


def request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/profiles/me/avatar",
            "headers": [],
            "client": ("192.0.2.10", 12345),
        }
    )


class PlatformMediaRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **overrides: object) -> PlatformSettings:
        values: dict[str, object] = {
            "_env_file": None,
            "platform_secret_key": "unit-test-secret-key-for-media-limits",
            "platform_media_rate_limit_enabled": True,
            "platform_media_upload_user_limit": 2,
            "platform_media_upload_ip_limit": 3,
            "platform_media_upload_user_byte_limit": 10 * 1024 * 1024,
        }
        values.update(overrides)
        return PlatformSettings(**values)

    async def test_user_attempt_limit_is_fixed_window_and_redis_is_closed(self) -> None:
        fake = FakeRedis()
        with patch(
            "python_packages.platform_infra.media_rate_limit.redis_client",
            return_value=fake,
        ):
            await check_media_upload_rate_limit(
                request(), user_id="user-1", upload_bytes=1024, settings=self.settings(), now_epoch=10
            )
            await check_media_upload_rate_limit(
                request(), user_id="user-1", upload_bytes=1024, settings=self.settings(), now_epoch=10
            )
            with self.assertRaises(HTTPException) as raised:
                await check_media_upload_rate_limit(
                    request(),
                    user_id="user-1",
                    upload_bytes=1024,
                    settings=self.settings(),
                    now_epoch=10,
                )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.detail["code"], "media_rate_limited")
        self.assertTrue(fake.closed)

    async def test_allowlisted_source_skips_ip_budget_but_keeps_user_and_byte_budgets(self) -> None:
        fake = FakeRedis()
        settings = self.settings(
            platform_load_test_source_ips="192.0.2.10",
            platform_media_upload_user_limit=2,
            platform_media_upload_ip_limit=1,
        )
        with patch(
            "python_packages.platform_infra.media_rate_limit.redis_client",
            return_value=fake,
        ):
            await check_media_upload_rate_limit(
                request(), user_id="user-1", upload_bytes=1024, settings=settings, now_epoch=10
            )
            await check_media_upload_rate_limit(
                request(), user_id="user-1", upload_bytes=1024, settings=settings, now_epoch=10
            )
            with self.assertRaises(HTTPException) as raised:
                await check_media_upload_rate_limit(
                    request(), user_id="user-1", upload_bytes=1024, settings=settings, now_epoch=10
                )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(fake.ip_count, 0)


if __name__ == "__main__":
    unittest.main()
