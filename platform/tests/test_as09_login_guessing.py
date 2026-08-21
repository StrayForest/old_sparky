from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request

from python_packages.platform_infra import auth_rate_limit
from python_packages.platform_infra.auth_rate_limit import (
    check_login_rate_limit,
    clear_login_failures,
    record_login_failure,
)
from python_packages.platform_infra.config import PlatformSettings


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.expires_at: dict[str, int] = {}
        self.now_epoch = 0

    def _expire(self, key: str) -> None:
        expires_at = self.expires_at.get(key)
        if expires_at is not None and expires_at <= self.now_epoch:
            self.values.pop(key, None)
            self.expires_at.pop(key, None)

    async def eval(
        self,
        script: str,
        key_count: int,
        key: str,
        argument: int,
    ) -> int:
        self.assert_key_count(key_count)
        self._expire(key)
        if script == auth_rate_limit.FIXED_WINDOW_INCREMENT_SCRIPT:
            self.values[key] = self.values.get(key, 0) + 1
            self.expires_at[key] = argument
            return self.values[key]
        if script == auth_rate_limit.DELIVERY_COOLDOWN_SCRIPT:
            if key not in self.values:
                self.values[key] = 1
                self.expires_at[key] = self.now_epoch + argument
                return 0
            return max(1, self.expires_at[key] - self.now_epoch)
        raise AssertionError("unexpected Redis script")

    @staticmethod
    def assert_key_count(key_count: int) -> None:
        if key_count != 1:
            raise AssertionError(f"unexpected key count: {key_count}")

    async def get(self, key: str) -> int | None:
        self._expire(key)
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.expires_at.pop(key, None)

    async def aclose(self) -> None:
        return None


class DistributedLoginGuessingTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, **overrides: object) -> PlatformSettings:
        values: dict[str, object] = {
            "_env_file": None,
            "platform_secret_key": "as09-unit-test-secret-key",
            "platform_auth_rate_limit_enabled": True,
            "platform_auth_login_window_seconds": 60,
            "platform_auth_login_account_limit": 2,
            "platform_auth_login_account_cooldown_seconds": 30,
            "platform_auth_login_ip_limit": 100,
            "platform_auth_adaptive_turnstile_threshold": 2,
            "platform_auth_progressive_delay_base_seconds": 0,
        }
        values.update(overrides)
        return PlatformSettings(**values)

    @staticmethod
    def _request(address: str) -> Request:
        return Request(
            {
                "type": "http",
                "headers": [],
                "client": (address, 1234),
            }
        )

    async def _checked_failure(
        self,
        cache: _FakeRedis,
        *,
        address: str,
        email: str,
        settings: PlatformSettings,
        now_epoch: int,
    ) -> None:
        cache.now_epoch = now_epoch
        await check_login_rate_limit(
            self._request(address),
            email,
            settings=settings,
            now_epoch=now_epoch,
        )
        await record_login_failure(
            self._request(address),
            email,
            settings=settings,
            now_epoch=now_epoch,
        )

    async def test_failures_follow_account_across_source_ips_and_start_cooldown(self) -> None:
        settings = self._settings()
        cache = _FakeRedis()
        email = "Player@Example.com"

        with patch.object(auth_rate_limit, "redis_client", return_value=cache):
            await self._checked_failure(
                cache,
                address="192.0.2.10",
                email=email,
                settings=settings,
                now_epoch=125,
            )
            await self._checked_failure(
                cache,
                address="198.51.100.20",
                email=email,
                settings=settings,
                now_epoch=126,
            )
            cache.now_epoch = 127
            state = await check_login_rate_limit(
                self._request("203.0.113.30"),
                email,
                settings=settings,
                now_epoch=127,
            )
            self.assertTrue(state.adaptive_turnstile_required)
            with self.assertRaises(HTTPException) as raised:
                await record_login_failure(
                    self._request("203.0.113.30"),
                    email,
                    settings=settings,
                    now_epoch=127,
                )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers["Retry-After"], "30")
        account_failure_keys = [
            key for key in cache.values if ":login-failure:" in key
        ]
        self.assertEqual(account_failure_keys, [])
        self.assertNotIn("player@example.com", " ".join(cache.values).lower())
        self.assertNotIn("192.0.2.10", " ".join(cache.values))

    async def test_active_cooldown_blocks_before_new_guess_and_does_not_extend(self) -> None:
        settings = self._settings()
        cache = _FakeRedis()
        email = "player@example.com"

        with patch.object(auth_rate_limit, "redis_client", return_value=cache):
            await self._checked_failure(
                cache,
                address="192.0.2.1",
                email=email,
                settings=settings,
                now_epoch=125,
            )
            await self._checked_failure(
                cache,
                address="192.0.2.2",
                email=email,
                settings=settings,
                now_epoch=126,
            )
            cache.now_epoch = 127
            with self.assertRaises(HTTPException):
                await record_login_failure(
                    self._request("192.0.2.3"),
                    email,
                    settings=settings,
                    now_epoch=127,
                )

            cooldown_key = next(
                key for key in cache.values if ":login-cooldown:" in key
            )
            original_expiry = cache.expires_at[cooldown_key]

            cache.now_epoch = 140
            with self.assertRaises(HTTPException) as blocked:
                await check_login_rate_limit(
                    self._request("198.51.100.40"),
                    email,
                    settings=settings,
                    now_epoch=140,
                )
            self.assertEqual(blocked.exception.status_code, 429)
            self.assertEqual(cache.expires_at[cooldown_key], original_expiry)

            cache.now_epoch = 158
            state = await check_login_rate_limit(
                self._request("198.51.100.41"),
                email,
                settings=settings,
                now_epoch=158,
            )
            self.assertFalse(state.adaptive_turnstile_required)
            await record_login_failure(
                self._request("198.51.100.41"),
                email,
                settings=settings,
                now_epoch=158,
            )
            cache.now_epoch = 159
            await record_login_failure(
                self._request("198.51.100.42"),
                email,
                settings=settings,
                now_epoch=159,
            )
            cache.now_epoch = 160
            state = await check_login_rate_limit(
                self._request("198.51.100.43"),
                email,
                settings=settings,
                now_epoch=160,
            )
            self.assertTrue(state.adaptive_turnstile_required)
            with self.assertRaises(HTTPException) as retriggered:
                await record_login_failure(
                    self._request("198.51.100.43"),
                    email,
                    settings=settings,
                    now_epoch=160,
                )
            self.assertEqual(retriggered.exception.status_code, 429)
            self.assertEqual(cache.expires_at[cooldown_key], 190)

    async def test_success_reset_clears_account_window_and_cooldown(self) -> None:
        settings = self._settings()
        cache = _FakeRedis()
        email = "player@example.com"
        request = self._request("192.0.2.50")

        with patch.object(auth_rate_limit, "redis_client", return_value=cache):
            for now_epoch, address in (
                (125, "192.0.2.50"),
                (126, "198.51.100.50"),
            ):
                await self._checked_failure(
                    cache,
                    address=address,
                    email=email,
                    settings=settings,
                    now_epoch=now_epoch,
                )
            cache.now_epoch = 127
            with self.assertRaises(HTTPException):
                await record_login_failure(
                    self._request("203.0.113.50"),
                    email,
                    settings=settings,
                    now_epoch=127,
                )

            await clear_login_failures(
                request,
                email,
                settings=settings,
                now_epoch=127,
            )
            cache.now_epoch = 128
            state = await check_login_rate_limit(
                self._request("203.0.113.51"),
                email,
                settings=settings,
                now_epoch=128,
            )

        self.assertFalse(state.adaptive_turnstile_required)
        self.assertFalse(any(":login-cooldown:" in key for key in cache.values))
        self.assertFalse(any(":login-failure:" in key for key in cache.values))

    async def test_known_and_missing_identifiers_share_private_key_shape(self) -> None:
        settings = self._settings(platform_auth_login_account_limit=1)
        cache = _FakeRedis()

        with patch.object(auth_rate_limit, "redis_client", return_value=cache):
            for index, email in enumerate(
                ("known@example.com", "missing@example.com"),
                start=1,
            ):
                await self._checked_failure(
                    cache,
                    address=f"192.0.2.{index}",
                    email=email,
                    settings=settings,
                    now_epoch=125,
                )
                cache.now_epoch = 126
                with self.assertRaises(HTTPException) as raised:
                    await record_login_failure(
                        self._request(f"198.51.100.{index}"),
                        email,
                        settings=settings,
                        now_epoch=126,
                    )
                self.assertEqual(raised.exception.status_code, 429)

        serialized_keys = " ".join(cache.values).lower()
        self.assertNotIn("known@example.com", serialized_keys)
        self.assertNotIn("missing@example.com", serialized_keys)
        self.assertEqual(
            len([key for key in cache.values if ":login-cooldown:" in key]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
