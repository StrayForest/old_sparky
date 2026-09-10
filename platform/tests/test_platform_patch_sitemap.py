from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.api.routes import content as content_routes
from apps.platform_api.app.services import home_content, home_content_security
from python_packages.platform_infra.config import PlatformSettings
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class _Pipeline:
    def __init__(self, cache: "_Cache") -> None:
        self.cache = cache
        self.set_calls: list[tuple[tuple, dict]] = []

    async def __aenter__(self) -> "_Pipeline":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        return self

    async def execute(self) -> list[bool]:
        return [True] * len(self.set_calls)


class _Cache:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}
        self.set_calls: list[tuple[tuple, dict]] = []
        self.pipeline_instance = _Pipeline(self)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, *args, **kwargs) -> bool:
        self.set_calls.append((args, kwargs))
        if args and args[0] == home_content.PATCH_SITEMAP_INDEX_KEY:
            self.values[args[0]] = str(args[1])
        return True

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        return self.pipeline_instance

    async def eval(self, *args, **kwargs) -> int:
        return 1

    async def aclose(self) -> None:
        return None


def _patch_summary(patch_id: str, day: int) -> dict[str, str]:
    return {
        "id": patch_id,
        "title": f"Patch {patch_id}",
        "excerpt": "Patch summary",
        "published_at": f"2026-09-{day:02d}T12:00:00+00:00",
        "url": f"https://store.steampowered.com/news/app/1422450/view/{patch_id}",
    }


def _patch_detail(patch_id: str, day: int) -> dict[str, object]:
    return {
        **_patch_summary(patch_id, day),
        "content": "Damage increased from 10 to 12",
        "sections": [{
            "kind": "general",
            "title": "Общие изменения",
            "hero_name": None,
            "changes": ["Damage increased from 10 to 12"],
            "abilities": [],
        }],
    }


class PatchSitemapProjectionTests(PlatformIsolatedAsyncioTestCase):
    def test_projection_is_bounded_to_current_home_patch_order(self) -> None:
        patches = [_patch_summary(str(index), index) for index in range(1, 6)]
        patches.insert(2, patches[0])

        self.assertEqual(
            home_content.patch_sitemap_entries(patches),
            [
                {"id": "1", "published_at": "2026-09-01T12:00:00+00:00"},
                {"id": "2", "published_at": "2026-09-02T12:00:00+00:00"},
                {"id": "3", "published_at": "2026-09-03T12:00:00+00:00"},
                {"id": "4", "published_at": "2026-09-04T12:00:00+00:00"},
            ],
        )

    async def test_read_endpoint_only_reads_ready_state_and_sets_cache_headers(self) -> None:
        cache = _Cache({
            home_content.PATCH_SITEMAP_INDEX_KEY: json.dumps([
                {"id": "4", "published_at": "2026-09-04T12:00:00+00:00"},
            ])
        })
        refresh = AsyncMock(side_effect=AssertionError("sitemap must not refresh content"))
        with (
            patch.object(home_content, "redis_client", return_value=cache),
            patch.object(home_content, "refresh_home_content", refresh),
        ):
            response = content_routes.Response()
            payload = await content_routes.patch_sitemap_index(response)

        self.assertEqual([entry.id for entry in payload.patches], ["4"])
        self.assertEqual(
            response.headers["cache-control"],
            "public, max-age=60, stale-while-revalidate=300",
        )
        refresh.assert_not_awaited()
        self.assertEqual(cache.set_calls, [])

    async def test_read_endpoint_recovers_from_ready_home_cache_after_index_loss(self) -> None:
        patches = [_patch_summary(str(index), index) for index in range(1, 6)]
        cache = _Cache({
            home_content.HOME_CONTENT_KEY: json.dumps({"patches": patches}),
        })
        with patch.object(home_content, "redis_client", return_value=cache):
            response = content_routes.Response()
            payload = await content_routes.patch_sitemap_index(response)

        self.assertEqual([entry.id for entry in payload.patches], ["1", "2", "3", "4"])
        self.assertEqual(cache.set_calls, [])

    async def test_successful_refresh_updates_index_after_translation_registration(self) -> None:
        patches = [_patch_summary(str(index), index) for index in range(1, 5)]
        details = {patch["id"]: _patch_detail(patch["id"], index) for index, patch in enumerate(patches, 1)}
        cache = _Cache()
        events: list[str] = []

        async def register(_details):
            events.append("translation")
            return {"registered": 4, "enqueued": 1, "enqueue_failures": 0}

        async def publish(current_patches):
            events.append("sitemap")
            self.assertEqual(current_patches, patches)
            return True

        settings = PlatformSettings(_env_file=None)
        with (
            patch.object(home_content_security, "redis_client", return_value=cache),
            patch.object(home_content, "redis_client", return_value=cache),
            patch.object(home_content_security, "get_settings", return_value=settings),
            patch.object(home_content_security, "BoundedNoRedirectAsyncClient", return_value=_ClientContext()),
            patch.object(home_content, "_fetch_steam_patches", new=AsyncMock(return_value=(patches, details))),
            patch.object(home_content, "_fetch_youtube_videos", new=AsyncMock(return_value=[])),
            patch.object(home_content, "_fetch_deadlock_asset_catalog", new=AsyncMock(return_value={})),
            patch.object(home_content_security, "ensure_patch_translation_records", new=AsyncMock(side_effect=register)),
            patch.object(home_content, "publish_patch_sitemap_index", new=AsyncMock(side_effect=publish)),
        ):
            payload = await home_content_security.refresh_home_content(force=True)

        self.assertEqual(payload["patches"], patches)
        self.assertEqual(events, ["translation", "sitemap"])

    async def test_failed_steam_refresh_keeps_known_good_index(self) -> None:
        previous = [{"id": "999", "published_at": "2026-08-31T12:00:00+00:00"}]
        cache = _Cache({home_content.PATCH_SITEMAP_INDEX_KEY: json.dumps(previous)})
        settings = PlatformSettings(_env_file=None)
        publish = AsyncMock(side_effect=AssertionError("failed Steam refresh must not publish"))
        with (
            patch.object(home_content_security, "redis_client", return_value=cache),
            patch.object(home_content, "redis_client", return_value=cache),
            patch.object(home_content_security, "get_settings", return_value=settings),
            patch.object(home_content_security, "BoundedNoRedirectAsyncClient", return_value=_ClientContext()),
            patch.object(home_content, "_fetch_steam_patches", new=AsyncMock(side_effect=ValueError("Steam unavailable"))),
            patch.object(home_content, "_fetch_youtube_videos", new=AsyncMock(return_value=[])),
            patch.object(home_content, "_fetch_deadlock_asset_catalog", new=AsyncMock(return_value={})),
            patch.object(home_content, "publish_patch_sitemap_index", new=publish),
        ):
            await home_content_security.refresh_home_content(force=True)

        self.assertEqual(json.loads(cache.values[home_content.PATCH_SITEMAP_INDEX_KEY]), previous)
        publish.assert_not_awaited()


class _ClientContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
