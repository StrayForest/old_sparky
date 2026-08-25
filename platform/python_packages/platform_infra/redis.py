from __future__ import annotations

from redis.asyncio import Redis
from redis.asyncio import from_url

from python_packages.platform_infra.config import get_settings


def redis_client() -> Redis:
    return from_url(
        get_settings().platform_redis_url,
        decode_responses=True,
    )


async def warm_up_redis() -> None:
    client = redis_client()
    try:
        await client.ping()
    finally:
        await client.aclose()


async def dispose_redis() -> None:
    from python_packages.platform_infra.sse_connection_limit import (
        dispose_sse_connection_limiter,
    )

    await dispose_sse_connection_limiter()
