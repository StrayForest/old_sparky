from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from python_packages.platform_infra.redis import redis_client

logger = logging.getLogger(__name__)


def bracket_channel(tournament_id: str) -> str:
    return f"platform:bracket:{tournament_id}"


async def publish_bracket_event(
    tournament_id: str,
    payload: dict[str, Any],
) -> None:
    client = redis_client()
    try:
        await client.publish(
            bracket_channel(tournament_id),
            json.dumps(payload, separators=(",", ":")),
        )
    except Exception:
        logger.exception(
            "Failed to publish bracket event for tournament %s.",
            tournament_id,
        )
    finally:
        await client.aclose()


async def stream_bracket_events(tournament_id: str) -> AsyncIterator[str]:
    client = redis_client()
    pubsub = client.pubsub()
    channel = bracket_channel(tournament_id)
    await pubsub.subscribe(channel)
    try:
        yield "event: connected\ndata: {}\n\n"
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=15.0,
            )
            if message is None:
                yield ": keepalive\n\n"
                continue
            yield f"event: bracket\ndata: {message['data']}\n\n"
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await client.aclose()
