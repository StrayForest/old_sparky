from __future__ import annotations

import asyncio

from redis.asyncio import Redis
from redis.asyncio import from_url

from python_packages.platform_infra.config import get_settings


_shared_clients: dict[bool, tuple[asyncio.AbstractEventLoop | None, Redis]] = {}


def redis_client(
    *,
    decode_responses: bool = True,
    shared: bool = False,
) -> Redis:
    """Return an owned client or a process-local pooled client."""

    if not shared:
        return from_url(
            get_settings().platform_redis_url,
            decode_responses=decode_responses,
        )

    try:
        current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    entry = _shared_clients.get(decode_responses)
    if entry is not None and entry[0] is current_loop:
        return entry[1]

    if entry is not None:
        # A new loop can only occur in isolated tests or an explicitly
        # recreated runtime. Do not await the old client's pool from this
        # synchronous factory; its owning loop may already be closed.
        _shared_clients.pop(decode_responses, None)

    if entry is None or entry[0] is not current_loop:
        client = from_url(
            get_settings().platform_redis_url,
            decode_responses=decode_responses,
            max_connections=get_settings().platform_redis_max_connections,
        )
        _shared_clients[decode_responses] = (current_loop, client)
    return client


async def warm_up_redis() -> None:
    for decode_responses in (True, False):
        client = redis_client(
            decode_responses=decode_responses,
            shared=True,
        )
        await client.ping()


async def dispose_redis_clients() -> None:
    """Close process-local Redis pools during application shutdown."""

    clients = tuple(client for _loop, client in _shared_clients.values())
    _shared_clients.clear()
    for client in clients:
        await client.aclose()
