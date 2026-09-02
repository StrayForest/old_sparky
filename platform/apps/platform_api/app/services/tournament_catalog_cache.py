"""Short-lived Redis response cache for the anonymous tournament catalog."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
from time import perf_counter
from typing import Any

from redis.exceptions import RedisError

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.performance import record_redis_read_model_event
from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)

PUBLIC_TOURNAMENT_LIST_CACHE_KEY_PREFIX = "platform:tournament:list:v1"
PUBLIC_TOURNAMENT_LIST_CACHE_TTL_SECONDS = 5
_REDIS_UNAVAILABLE = (RedisError, OSError, asyncio.TimeoutError)


@dataclass(frozen=True, slots=True)
class PublicTournamentListCacheEntry:
    body: bytes
    limit: int
    has_more: bool
    next_cursor: str | None


def public_tournament_list_cache_key(
    *,
    search: str | None,
    rank: list[str],
    open_registration: bool,
    status_filter: str | None,
    participants_sort: str | None,
    date_sort: str | None,
    limit: int,
    cursor: str | None,
) -> str:
    """Return a privacy-safe key for one public query representation."""

    identity = {
        "search": (search or "").strip().lower(),
        "rank": sorted(str(item).strip().lower() for item in rank if str(item).strip()),
        "open_registration": bool(open_registration),
        "status": status_filter or "",
        "participants_sort": participants_sort or "",
        "date_sort": date_sort or "",
        "limit": int(limit),
        "cursor": cursor or "",
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{PUBLIC_TOURNAMENT_LIST_CACHE_KEY_PREFIX}:{digest}"


def _cache_enabled() -> bool:
    # Test databases intentionally exercise authoritative SQL and must not
    # leak a response between isolated test cases. Development and production
    # retain the same Redis-backed behavior.
    return get_settings().platform_environment.strip().lower() != "test"


def _decode_entry(raw: bytes | str | None) -> PublicTournamentListCacheEntry | None:
    if raw is None:
        return None
    try:
        value: Any = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        if not isinstance(value, dict) or value.get("v") != 1:
            return None
        items = value.get("items")
        limit = value.get("limit")
        has_more = value.get("has_more")
        next_cursor = value.get("next_cursor")
        if (
            not isinstance(items, list)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or not isinstance(has_more, bool)
            or (next_cursor is not None and not isinstance(next_cursor, str))
        ):
            return None
        body = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return PublicTournamentListCacheEntry(
            body=body,
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
        )
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


async def get_public_tournament_list_cache(
    key: str,
) -> PublicTournamentListCacheEntry | None:
    if not _cache_enabled():
        return None
    started_at = perf_counter()
    client = redis_client(decode_responses=False, shared=True)
    try:
        entry = _decode_entry(await client.get(key))
        record_redis_read_model_event(
            model="tournament_list_cache",
            outcome="hit" if entry is not None else "miss",
            get_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=len(entry.body) if entry is not None else 0,
        )
        return entry
    except _REDIS_UNAVAILABLE as exc:
        record_redis_read_model_event(
            model="tournament_list_cache",
            outcome="error",
            get_ms=(perf_counter() - started_at) * 1000,
        )
        logger.warning("Redis tournament list GET failed error=%s", type(exc).__name__)
        return None


async def set_public_tournament_list_cache(
    key: str,
    *,
    body: bytes,
    limit: int,
    has_more: bool,
    next_cursor: str | None,
) -> None:
    if not _cache_enabled():
        return
    try:
        items = json.loads(body.decode("utf-8"))
        if not isinstance(items, list):
            return
        value = json.dumps(
            {
                "v": 1,
                "items": items,
                "limit": int(limit),
                "has_more": bool(has_more),
                "next_cursor": next_cursor,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return

    started_at = perf_counter()
    client = redis_client(decode_responses=False, shared=True)
    try:
        await client.set(key, value, ex=PUBLIC_TOURNAMENT_LIST_CACHE_TTL_SECONDS)
        record_redis_read_model_event(
            model="tournament_list_cache",
            outcome="write",
            set_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=len(body),
        )
    except _REDIS_UNAVAILABLE as exc:
        record_redis_read_model_event(
            model="tournament_list_cache",
            outcome="error",
            set_ms=(perf_counter() - started_at) * 1000,
            payload_bytes=len(body),
        )
        logger.warning("Redis tournament list SET failed error=%s", type(exc).__name__)
