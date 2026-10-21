from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from apps.platform_api.app.services import home_content, patch_detail_security
from apps.platform_api.app.services.patch_translation import translation_source_hash
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


_REFRESH_OBSERVER_TIMEOUT_SECONDS = 0.5


def _capture_refresh_tasks() -> tuple[list[asyncio.Task[None]], asyncio.Event, object]:
    tasks: list[asyncio.Task[None]] = []
    registered = asyncio.Event()
    original = patch_detail_security._track_background_refresh

    def capture(task: asyncio.Task[None]) -> None:
        tasks.append(task)
        original(task)
        registered.set()

    return tasks, registered, patch.object(
        patch_detail_security, "_track_background_refresh", side_effect=capture
    )


async def _await_refresh_tasks(tasks: list[asyncio.Task[None]]) -> None:
    if not tasks:
        return
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True),
        timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result


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


class PlatformPatchMissSecurityTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._refresh_baseline = set(patch_detail_security._BACKGROUND_REFRESH_TASKS)

    async def asyncTearDown(self) -> None:
        tasks = list(
            set(patch_detail_security._BACKGROUND_REFRESH_TASKS)
            - self._refresh_baseline
        )
        if tasks:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS,
            )
        self.assertEqual(
            set(patch_detail_security._BACKGROUND_REFRESH_TASKS),
            self._refresh_baseline,
        )

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
        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
            refresh_patch,
        ):
            result = await patch_detail_security._cached_patch_source("999")
            await asyncio.wait_for(registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks)

        self.assertIsNone(result)
        self.assertEqual(len(tasks), 1)
        refresh.assert_awaited_once()
        self.assertIn(
            patch_detail_security.PATCH_MISS_NEGATIVE_KEY_PREFIX + "999",
            cache.values,
        )
        self.assertIn(patch_detail_security.PATCH_MISS_REFRESH_GATE_KEY, cache.values)

    async def test_negative_cache_prevents_repeat_refresh_for_same_patch(self) -> None:
        cache = _Cache()
        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
            refresh_patch,
        ):
            await patch_detail_security._cached_patch_source("999")
            await patch_detail_security._cached_patch_source("999")
            await asyncio.wait_for(registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks)

        self.assertEqual(len(tasks), 1)
        refresh.assert_awaited_once()

    async def test_distinct_unknown_ids_share_one_global_refresh_gate(self) -> None:
        cache = _Cache()
        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as refresh,
            refresh_patch,
        ):
            await patch_detail_security._cached_patch_source("901")
            await patch_detail_security._cached_patch_source("902")
            await asyncio.wait_for(registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks)

        self.assertEqual(len(tasks), 1)
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

    async def test_legacy_patch_id_reprocesses_cached_source_without_latest_steam_feed(self) -> None:
        patch_id = "987654321"
        old_detail = {
            "id": patch_id,
            "title": "Historical Patch",
            "content": """[General]
Baba: Threadsap - Cooldown increased from 26s to 28s
""",
            "sections": [
                {
                    "kind": "general",
                    "title": "Общие изменения",
                    "changes": ["Baba: Threadsap - Cooldown increased from 26s to 28s"],
                    "abilities": [],
                }
            ],
        }
        old_source_hash = translation_source_hash(old_detail)
        catalog = {
            "heroes": {
                "baba": {
                    "id": 88,
                    "name": "Baba",
                    "slug": "hero_baba",
                    "abilities": {
                        "threadsap": {"id": 8801, "name": "Threadsap"},
                    },
                }
            },
            "items": {},
            "objectives": {},
        }
        legacy_key = patch_detail_security.LEGACY_PATCH_DETAIL_KEY_PREFIXES[0] + patch_id
        cache = _Cache(
            {
                legacy_key: json.dumps(old_detail),
                home_content.PATCH_ASSET_CATALOG_STALE_KEY: json.dumps(catalog),
            }
        )
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "ensure_patch_translation_records",
                AsyncMock(return_value={"registered": 1, "enqueued": 1, "enqueue_failures": 0}),
            ) as register,
            patch.object(
                patch_detail_security,
                "_refresh_legacy_patch_after_miss",
                AsyncMock(),
            ) as legacy_refresh,
            patch.object(
                patch_detail_security,
                "_refresh_patch_cache_after_miss",
                AsyncMock(),
            ) as steam_refresh,
        ):
            result = await patch_detail_security._cached_patch_source(patch_id)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["id"], patch_id)
        self.assertEqual(result["sections"][0]["kind"], "hero")
        self.assertEqual(result["sections"][0]["hero_name"], "Baba")
        self.assertEqual(
            result["sections"][0]["abilities"][0]["changes"],
            ["Cooldown increased from 26s to 28s"],
        )
        self.assertNotEqual(old_source_hash, translation_source_hash(result))
        self.assertIn(home_content.PATCH_DETAIL_KEY_PREFIX + patch_id, cache.values)
        self.assertNotIn(home_content.PATCH_ASSET_CATALOG_KEY, cache.values)
        register.assert_awaited_once()
        self.assertIn(patch_id, register.await_args.args[0])
        legacy_refresh.assert_not_awaited()
        steam_refresh.assert_not_awaited()

    async def test_legacy_patch_with_cold_catalog_schedules_exact_id_reprocessing(self) -> None:
        patch_id = "987654322"
        old_detail = {"id": patch_id, "content": "Baba: Threadsap - Cooldown increased"}
        cache = _Cache(
            {
                patch_detail_security.LEGACY_PATCH_DETAIL_KEY_PREFIXES[0] + patch_id:
                    json.dumps(old_detail)
            }
        )
        tasks, registered, refresh_patch = _capture_refresh_tasks()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "_cached_asset_catalog",
                AsyncMock(return_value=None),
            ),
            patch.object(
                patch_detail_security,
                "_refresh_legacy_patch_after_miss",
                AsyncMock(),
            ) as refresh_legacy,
            refresh_patch,
        ):
            results = await asyncio.gather(
                patch_detail_security._cached_patch_source(patch_id),
                patch_detail_security._cached_patch_source(patch_id),
            )
            await asyncio.wait_for(registered.wait(), timeout=_REFRESH_OBSERVER_TIMEOUT_SECONDS)
            await _await_refresh_tasks(tasks)

        self.assertEqual(results, [None, None])
        self.assertEqual(len(tasks), 1)
        refresh_legacy.assert_awaited_once_with(patch_id, old_detail)
        self.assertIn(
            patch_detail_security.LEGACY_PATCH_REFRESH_GATE_KEY_PREFIX + patch_id,
            cache.values,
        )

    async def test_patch_miss_refresh_registers_translations_after_cache_publish(self) -> None:
        raw_detail = {
            "id": "123",
            "title": "Patch",
            "content": "- Map speed reduced",
        }
        cache = _Cache()
        with (
            patch.object(patch_detail_security, "redis_client", return_value=cache),
            patch.object(
                patch_detail_security,
                "ensure_patch_translation_records",
                AsyncMock(return_value={"registered": 1, "enqueued": 1, "enqueue_failures": 0}),
            ) as register,
        ):
            await patch_detail_security._publish_patch_refresh(
                {"123": raw_detail},
                {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}},
                publish_catalog=False,
            )

        register.assert_awaited_once()
        registered_details = register.await_args.args[0]
        self.assertIn("123", registered_details)
        self.assertEqual(registered_details["123"]["id"], "123")
        self.assertTrue(registered_details["123"]["sections"])

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
