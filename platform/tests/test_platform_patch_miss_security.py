from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from apps.platform_api.app.services import home_content
from apps.platform_api.app.services import patch_detail_security


class _Pipeline:
    def __init__(self, cache: "_Cache") -> None:
        self.cache = cache
        self.pending: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def set(self, key: str, value: str, **kwargs):
        self.pending.append((key, value))
        return self

    async def execute(self) -> list[bool]:
        for key, value in self.pending:
            self.cache.values[key] = value
        return [True] * len(self.pending)


class _Cache:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.set_calls: list[tuple[str, str, dict]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs) -> bool:
        self.set_calls.append((key, value, kwargs))
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        self.assert_transaction = transaction
        return _Pipeline(self)

    async def aclose(self) -> None:
        return None


class PlatformPatchMissSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        tasks = list(patch_detail_security._BACKGROUND_REFRESH_TASKS)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_cached_patch_is_returned_without_miss_refresh(self) -> None:
        detail = {"id": "123", "title": "Patch", "sections": []}
        cache = _Cache(
            {
                home_content.PATCH_DETAIL_KEY_PREFIX + "123": json.dumps(detail),
            }
        )
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
        ):
            result = await patch_detail_security._cached_patch_source("123")

        self.assertEqual(result, detail)
        refresh.assert_not_awaited()

    async def test_unknown_patch_sets_negative_cache_and_schedules_background_refresh(self) -> None:
        cache = _Cache()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
        ):
            result = await patch_detail_security._cached_patch_source("999")
            await asyncio.sleep(0)

        self.assertIsNone(result)
        refresh.assert_awaited_once()
        self.assertIn(
            patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX + "999",
            cache.values,
        )
        self.assertIn(patch_detail_security.PATCH_MISS_REFRESH_GATE_KEY, cache.values)

    async def test_negative_cache_prevents_repeat_refresh_for_same_patch(self) -> None:
        cache = _Cache()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
        ):
            await patch_detail_security._cached_patch_source("999")
            await patch_detail_security._cached_patch_source("999")
            await asyncio.sleep(0)

        refresh.assert_awaited_once()

    async def test_distinct_unknown_ids_share_one_global_refresh_gate(self) -> None:
        cache = _Cache()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
        ):
            await patch_detail_security._cached_patch_source("901")
            await patch_detail_security._cached_patch_source("902")
            await asyncio.sleep(0)

        refresh.assert_awaited_once()
        negative_keys = {
            key
            for key in cache.values
            if key.startswith(patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX)
        }
        self.assertEqual(
            negative_keys,
            {
                patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX + "901",
                patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX + "902",
            },
        )

    async def test_invalid_patch_id_does_not_touch_cache(self) -> None:
        with patch.object(patch_detail_security, "redis_client") as redis_factory:
            result = await patch_detail_security._cached_patch_source("not-a-patch")

        self.assertIsNone(result)
        redis_factory.assert_not_called()

    async def test_existing_structured_patch_does_not_force_asset_refresh(self) -> None:
        detail = {
            "id": "123",
            "title": "Patch",
            "content": "Map changed",
            "sections": [{"kind": "general", "title": "General", "changes": []}],
        }
        cache = _Cache(
            {
                home_content.PATCH_DETAIL_KEY_PREFIX + "123": json.dumps(detail),
            }
        )
        translated = {**detail, "translated": True}
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "apply_cached_patch_translation",
                AsyncMock(return_value=translated),
            ) as translate,
        ):
            result = await patch_detail_security.get_patch_detail("123")

        self.assertEqual(result, translated)
        translate.assert_awaited_once_with(detail)

    async def test_bounded_client_does_not_follow_redirects(self) -> None:
        requests: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "https://redirected.example/patch"},
                request=request,
            )

        async with patch_detail_security._BoundedPatchRefreshClient(
            transport=httpx.MockTransport(handler),
            max_response_bytes=64,
        ) as client:
            response = await client.get("https://api.steampowered.com/example")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(requests, ["https://api.steampowered.com/example"])

    async def test_bounded_client_rejects_oversized_response(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 65, request=request)

        async with patch_detail_security._BoundedPatchRefreshClient(
            transport=httpx.MockTransport(handler),
            max_response_bytes=64,
        ) as client:
            with self.assertRaisesRegex(ValueError, "byte limit"):
                await client.get("https://api.steampowered.com/example")


if __name__ == "__main__":
    unittest.main()
