from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from apps.platform_api.app.services import home_content as base
from apps.platform_api.app.services.patch_detail import structure_patch_detail
from apps.platform_api.app.services.patch_translation import (
    apply_cached_patch_translation,
    ensure_patch_translation_records,
)
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client


logger = logging.getLogger(__name__)

PATCH_MISS_NEGATIVE_KEY_PREFIX = "platform:home-content:patch-miss:v1:"
PATCH_MISS_REFRESH_GATE_KEY = "platform:home-content:patch-miss-refresh:v1"
PATCH_MISS_NEGATIVE_TTL_SECONDS = 5 * 60
PATCH_MISS_REFRESH_GATE_SECONDS = 60
PATCH_MISS_RESPONSE_MAX_BYTES = 8 * 1024 * 1024

_BACKGROUND_REFRESH_TASKS: set[asyncio.Task[None]] = set()


class _BoundedPatchRefreshClient(httpx.AsyncClient):
    """HTTP client for patch-miss refreshes with fail-closed response bounds."""

    def __init__(self, *args: Any, max_response_bytes: int = PATCH_MISS_RESPONSE_MAX_BYTES, **kwargs: Any) -> None:
        kwargs["follow_redirects"] = False
        super().__init__(*args, **kwargs)
        self._max_response_bytes = max_response_bytes

    async def get(self, url: Any, **kwargs: Any) -> httpx.Response:
        kwargs["follow_redirects"] = False
        async with self.stream("GET", url, **kwargs) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > self._max_response_bytes:
                    raise ValueError("External patch refresh response exceeded the byte limit.")
                body.extend(chunk)

            headers = httpx.Headers(
                (name, value)
                for name, value in response.headers.multi_items()
                if name.lower() not in {"content-encoding", "content-length"}
            )
            return httpx.Response(
                status_code=response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
                extensions=response.extensions,
            )


def _decode_mapping(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


async def _cached_asset_catalog() -> dict[str, Any] | None:
    cache = redis_client()
    try:
        return _decode_mapping(await cache.get(base.PATCH_ASSET_CATALOG_KEY))
    finally:
        await cache.aclose()


def _track_background_refresh(task: asyncio.Task[None]) -> None:
    _BACKGROUND_REFRESH_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_REFRESH_TASKS.discard)


async def _publish_patch_refresh(
    details: dict[str, dict[str, Any]],
    catalog: dict[str, Any],
    *,
    publish_catalog: bool,
) -> None:
    cache = redis_client()
    structured_details: dict[str, dict[str, Any]] = {}
    try:
        async with cache.pipeline(transaction=True) as pipeline:
            if publish_catalog:
                pipeline.set(
                    base.PATCH_ASSET_CATALOG_KEY,
                    json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
                    ex=base.PATCH_ASSET_CATALOG_TTL_SECONDS,
                )
            for patch_id, raw_detail in details.items():
                structured = structure_patch_detail(raw_detail, catalog)
                structured_details[patch_id] = structured
                pipeline.set(
                    base.PATCH_DETAIL_KEY_PREFIX + patch_id,
                    json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
                    ex=base.PATCH_DETAIL_TTL_SECONDS,
                )
            await pipeline.execute()
    finally:
        await cache.aclose()
    if structured_details:
        try:
            translation_registration = await ensure_patch_translation_records(
                structured_details
            )
            if translation_registration["enqueue_failures"]:
                logger.error(
                    "patch_miss_translation_registration_degraded registered=%s enqueued=%s failures=%s",
                    translation_registration["registered"],
                    translation_registration["enqueued"],
                    translation_registration["enqueue_failures"],
                )
        except Exception:
            logger.exception("patch_miss_translation_registration_failed")


async def _refresh_patch_cache_after_miss() -> None:
    try:
        catalog = await _cached_asset_catalog()
        settings = get_settings()
        timeout = httpx.Timeout(settings.platform_external_content_timeout_seconds)
        async with _BoundedPatchRefreshClient(timeout=timeout) as client:
            _, details = await base._fetch_steam_patches(client)
            publish_catalog = catalog is None
            if catalog is None:
                catalog = await base._fetch_deadlock_asset_catalog(client)
        await _publish_patch_refresh(
            details,
            catalog,
            publish_catalog=publish_catalog,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "patch_miss_background_refresh_failed error=%s",
            type(error).__name__,
        )


async def _cached_patch_source(patch_id: str) -> dict[str, Any] | None:
    if not patch_id.isdigit() or len(patch_id) > 32:
        return None

    cache = redis_client()
    schedule_refresh = False
    try:
        cached = _decode_mapping(await cache.get(base.PATCH_DETAIL_KEY_PREFIX + patch_id))
        if cached is not None:
            return cached

        negative_key = PATCH_MISS_NEGATIVE_KEY_PREFIX + patch_id
        if await cache.get(negative_key):
            return None

        first_negative = bool(
            await cache.set(
                negative_key,
                "1",
                nx=True,
                ex=PATCH_MISS_NEGATIVE_TTL_SECONDS,
            )
        )
        if first_negative:
            schedule_refresh = bool(
                await cache.set(
                    PATCH_MISS_REFRESH_GATE_KEY,
                    "1",
                    nx=True,
                    ex=PATCH_MISS_REFRESH_GATE_SECONDS,
                )
            )
    finally:
        await cache.aclose()

    if schedule_refresh:
        _track_background_refresh(asyncio.create_task(_refresh_patch_cache_after_miss()))
    return None


async def get_patch_detail(patch_id: str) -> dict[str, Any] | None:
    source = await _cached_patch_source(patch_id)
    if source is None:
        return None

    catalog = await _cached_asset_catalog()
    if catalog is None and isinstance(source.get("sections"), list):
        structured = source
    else:
        structured = structure_patch_detail(
            source,
            catalog or {"heroes": {}, "items": {}, "ranks": {}, "objectives": {}},
        )
    return await apply_cached_patch_translation(structured)
