from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import re
from secrets import token_urlsafe
from typing import Any

import httpx

from apps.platform_api.app.services import home_content as base
from apps.platform_api.app.services.external_content_http import (
    BoundedNoRedirectAsyncClient,
)
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client


logger = logging.getLogger(__name__)
_PATCH_ID_RE = re.compile(r"^[0-9]{1,32}$")


@dataclass(frozen=True)
class _ContentRefreshResult:
    payload: dict[str, Any]
    patch_details: dict[str, dict[str, Any]]
    patches_available: bool


def _cached_result(payload: dict[str, Any]) -> _ContentRefreshResult:
    return _ContentRefreshResult(
        payload=payload,
        patch_details={},
        patches_available=payload.get("patches_available") is True,
    )


async def refresh_home_content(*, force: bool = False) -> dict[str, Any]:
    """Refresh public content and maintain the normal translation/sitemap path."""

    result = await _refresh_home_content(force=force)
    translation_registration_succeeded = True
    if result.patch_details:
        # Keep the translation subsystem out of the translation-free diagnostic
        # owner below.  Import lazily so merely importing this hardened module
        # cannot load or initialize OpenAI/Celery code.
        from apps.platform_api.app.services.patch_translation import (
            ensure_patch_translation_records,
        )

        try:
            translation_registration = await ensure_patch_translation_records(
                result.patch_details
            )
            if translation_registration["enqueue_failures"]:
                logger.error(
                    "home_content_translation_registration_degraded registered=%s enqueued=%s failures=%s",
                    translation_registration["registered"],
                    translation_registration["enqueued"],
                    translation_registration["enqueue_failures"],
                )
        except Exception:
            translation_registration_succeeded = False
            logger.exception("home_content_translation_registration_failed")
    if translation_registration_succeeded and result.patches_available and result.patch_details:
        await base.publish_patch_sitemap_index(result.payload["patches"])
    return result.payload


async def refresh_content_distribution(*, force: bool = False) -> dict[str, Any]:
    """Refresh patch/video distribution without translation or Celery side effects.

    Production diagnostics use this narrow owner.  It intentionally shares the
    bounded cache/upstream refresh implementation but never imports, registers,
    translates, or enqueues patch translations and never publishes the sitemap.
    """

    result = await _refresh_home_content(force=force)
    return result.payload


async def get_cached_patch_detail(patch_id: str) -> dict[str, Any] | None:
    """Read a structured cached patch detail without translation fallback."""

    if _PATCH_ID_RE.fullmatch(str(patch_id)) is None:
        return None
    cache = redis_client()
    try:
        encoded = await cache.get(base.PATCH_DETAIL_KEY_PREFIX + str(patch_id))
    finally:
        await cache.aclose()
    if not encoded:
        return None
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


async def _refresh_home_content(*, force: bool = False) -> _ContentRefreshResult:
    """Refresh public content through bounded, no-redirect upstream I/O."""

    settings = get_settings()
    cache = redis_client()
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        if not force:
            cached = await base._read_json(cache, base.HOME_CONTENT_KEY)
            if cached is not None:
                return _cached_result(cached)
        stale = await base._read_json(cache, base.HOME_CONTENT_STALE_KEY)
        acquired = bool(
            await cache.set(
                base.HOME_CONTENT_LOCK_KEY,
                lock_token,
                nx=True,
                ex=base.REFRESH_LOCK_SECONDS,
            )
        )
        if not acquired:
            if stale is not None:
                return _cached_result(stale)
            await asyncio.sleep(0.2)
            return _cached_result(
                await base._read_json(cache, base.HOME_CONTENT_KEY)
                or base._empty_home_content()
            )

        asset_catalog = await base._read_json(cache, base.PATCH_ASSET_CATALOG_KEY)
        timeout = httpx.Timeout(settings.platform_external_content_timeout_seconds)
        async with BoundedNoRedirectAsyncClient(timeout=timeout) as client:
            steam_result, youtube_result = await asyncio.gather(
                base._fetch_steam_patches(client),
                base._fetch_youtube_videos(client),
                return_exceptions=True,
            )
            stale_patch_ids = {
                str(patch.get("id") or "")
                for patch in (stale or {}).get("patches") or []
            }
            has_new_patch = (
                not isinstance(steam_result, Exception)
                and any(
                    str(patch.get("id") or "") not in stale_patch_ids
                    for patch in steam_result[0]
                )
            )
            asset_result = asset_catalog
            if asset_catalog is None or has_new_patch:
                try:
                    asset_result = await base._fetch_deadlock_asset_catalog(
                        client,
                        previous_catalog=asset_catalog,
                    )
                except Exception as error:  # noqa: BLE001
                    asset_result = error
            if has_new_patch and isinstance(asset_result, Exception):
                logger.warning(
                    "home_content_refresh_deferred source=deadlock-assets error=%s",
                    type(asset_result).__name__,
                )
                steam_result = ValueError("New patch asset catalog is unavailable.")

        stale_patches = list((stale or {}).get("patches") or [])
        stale_videos = list((stale or {}).get("videos") or [])
        patch_details: dict[str, dict[str, Any]] = {}
        if isinstance(steam_result, Exception):
            logger.warning(
                "home_content_refresh_failed source=steam error=%s",
                type(steam_result).__name__,
            )
            patches = stale_patches
        else:
            patches, raw_patch_details = steam_result
            resolved_catalog = asset_catalog if isinstance(asset_catalog, dict) else {}
            if not isinstance(asset_result, Exception):
                resolved_catalog = asset_result
            patch_details = {
                patch_id: base._structure_patch_detail(detail, resolved_catalog)
                for patch_id, detail in raw_patch_details.items()
            }

        if isinstance(youtube_result, Exception):
            logger.warning(
                "home_content_refresh_failed source=youtube error=%s",
                type(youtube_result).__name__,
            )
            videos = stale_videos
        else:
            videos = youtube_result

        payload = {
            "patches": patches,
            "videos": videos,
            "generated_at": datetime.now(UTC).isoformat(),
            "patches_available": not isinstance(steam_result, Exception),
            "videos_available": not isinstance(youtube_result, Exception),
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with cache.pipeline(transaction=True) as pipeline:
            pipeline.set(
                base.HOME_CONTENT_KEY,
                encoded,
                ex=settings.platform_home_content_cache_seconds,
            )
            pipeline.set(
                base.HOME_CONTENT_STALE_KEY,
                encoded,
                ex=settings.platform_home_content_stale_seconds,
            )
            if (
                (asset_catalog is None or has_new_patch)
                and not isinstance(asset_result, Exception)
            ):
                pipeline.set(
                    base.PATCH_ASSET_CATALOG_KEY,
                    json.dumps(
                        asset_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    ex=base.PATCH_ASSET_CATALOG_TTL_SECONDS,
                )
            for patch_id, content in patch_details.items():
                pipeline.set(
                    base.PATCH_DETAIL_KEY_PREFIX + patch_id,
                    json.dumps(
                        content,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    ex=base.PATCH_DETAIL_TTL_SECONDS,
                )
            await pipeline.execute()
        return _ContentRefreshResult(
            payload=payload,
            patch_details=patch_details,
            patches_available=not isinstance(steam_result, Exception),
        )
    finally:
        if acquired:
            await cache.eval(
                base.LOCK_RELEASE_SCRIPT,
                1,
                base.HOME_CONTENT_LOCK_KEY,
                lock_token,
            )
        await cache.aclose()


async def get_deadlock_asset_catalog() -> dict[str, Any]:
    """Return the cached catalog, refreshing it only through hardened I/O."""

    catalog = await base._cached_deadlock_asset_catalog()
    if catalog is None:
        await refresh_home_content(force=True)
        catalog = await base._cached_deadlock_asset_catalog()
    return catalog or {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}}
