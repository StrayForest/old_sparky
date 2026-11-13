"""Bounded Redis cache for the anonymous, database-owned site palette."""

from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.appearance_palette import (
    DEFAULT_APPEARANCE_COLORS,
    normalize_appearance_colors,
    resolve_appearance_colors,
)
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.models import AppearanceSettings
from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)
APPEARANCE_CACHE_KEY = "platform:appearance:palette:v1"
APPEARANCE_REVISION_KEY = "platform:appearance:palette:revision:v1"
APPEARANCE_CACHE_TTL_SECONDS = 60
APPEARANCE_LOCAL_FALLBACK_TTL_SECONDS = 2
_REDIS_ERRORS = (RedisError, OSError, asyncio.TimeoutError)
_local_value: tuple[float, dict[str, Any]] | None = None
_PUBLISH_SCRIPT = """
local incoming = tonumber(ARGV[1])
local floor = tonumber(redis.call('GET', KEYS[2]) or '0')
local current = redis.call('GET', KEYS[1])
if current then
  local ok, decoded = pcall(cjson.decode, current)
  if ok and decoded.revision then
    floor = math.max(floor, tonumber(decoded.revision) or 0)
  end
end
if incoming < floor then return 0 end
redis.call('SET', KEYS[2], incoming)
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


def _cache_enabled() -> bool:
    return get_settings().platform_environment.strip().lower() != "test"


def _safe_payload(colors: Any, revision: Any) -> dict[str, Any] | None:
    try:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            return None
        return {"colors": normalize_appearance_colors(colors), "revision": revision}
    except (TypeError, ValueError):
        return None


def _decode(raw: bytes | str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return _safe_payload(value.get("colors"), value.get("revision")) if isinstance(value, dict) and value.get("v") == 1 else None
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


async def _redis_get() -> dict[str, Any] | None:
    if not _cache_enabled():
        return None
    try:
        client = redis_client(decode_responses=False, shared=True)
        async with client.pipeline(transaction=False) as pipe:
            pipe.get(APPEARANCE_CACHE_KEY)
            pipe.get(APPEARANCE_REVISION_KEY)
            raw, floor_raw = await asyncio.wait_for(pipe.execute(), timeout=0.05)
        payload = _decode(raw)
        try:
            floor = int(floor_raw or 0)
        except (TypeError, ValueError):
            return None
        return payload if payload is not None and payload["revision"] >= floor else None
    except _REDIS_ERRORS as exc:
        logger.warning("Appearance palette Redis GET failed error=%s", type(exc).__name__)
        return None


async def _redis_publish(payload: dict[str, Any]) -> None:
    if not _cache_enabled():
        return
    try:
        encoded = json.dumps({"v": 1, **payload}, separators=(",", ":"))
        await asyncio.wait_for(
            redis_client(decode_responses=True, shared=True).eval(
                _PUBLISH_SCRIPT,
                2,
                APPEARANCE_CACHE_KEY,
                APPEARANCE_REVISION_KEY,
                int(payload["revision"]),
                encoded,
                APPEARANCE_CACHE_TTL_SECONDS,
            ),
            timeout=0.05,
        )
    except _REDIS_ERRORS as exc:
        logger.warning("Appearance palette Redis SET failed error=%s", type(exc).__name__)


async def load_appearance_settings(db: AsyncSession) -> dict[str, Any]:
    global _local_value
    cached = await _redis_get()
    if cached is not None:
        resolved = {"colors": resolve_appearance_colors(cached["colors"]), "revision": cached["revision"]}
        if _cache_enabled():
            _local_value = (monotonic(), resolved)
        return resolved

    if (
        _cache_enabled()
        and _local_value is not None
        and monotonic() - _local_value[0] < APPEARANCE_LOCAL_FALLBACK_TTL_SECONDS
    ):
        return _local_value[1]

    row = await db.scalar(select(AppearanceSettings).where(AppearanceSettings.id == 1))
    payload = _safe_payload(row.colors, row.revision) if row is not None else None
    if payload is None:
        payload = {"colors": dict(DEFAULT_APPEARANCE_COLORS), "revision": 1}
    await _redis_publish(payload)
    resolved = {"colors": resolve_appearance_colors(payload["colors"]), "revision": payload["revision"]}
    if _cache_enabled():
        _local_value = (monotonic(), resolved)
    return resolved


async def lock_appearance_settings(db: AsyncSession) -> AppearanceSettings:
    global _local_value
    row = await db.scalar(
        select(AppearanceSettings)
        .where(AppearanceSettings.id == 1)
        .with_for_update()
    )
    if row is None:
        row = AppearanceSettings(id=1, colors=dict(DEFAULT_APPEARANCE_COLORS), revision=1)
        db.add(row)
        await db.flush()
    _local_value = None
    return row


async def invalidate_appearance_cache(payload: dict[str, Any]) -> None:
    global _local_value
    _local_value = None
    if not _cache_enabled():
        return
    await _redis_publish(payload)
